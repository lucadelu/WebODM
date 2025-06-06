import rasterio
import re
import logging
import os
import subprocess
import numpy as np
import numexpr as ne
from django.contrib.gis.geos import GEOSGeometry
from rasterio.enums import ColorInterp
from rasterio.vrt import WarpedVRT
from rasterio.windows import Window, bounds as window_bounds
from rio_tiler.utils import has_alpha_band, linear_rescale
from rio_tiler.colormap import cmap as colormap, apply_cmap
from rio_tiler.errors import InvalidColorMapName
from app.api.hsvblend import hsv_blend
from app.api.hillshade import LightSource
from app.geoutils import geom_transform_wkt_bbox
from rio_tiler.io import COGReader
from rasterio.warp import calculate_default_transform, reproject, Resampling

logger = logging.getLogger('app.logger')

ZOOM_EXTRA_LEVELS = 3

def extension_for_export_format(export_format):
    extensions = {
        'gtiff': 'tif',
        'gtiff-rgb': 'tif',
    }
    return extensions.get(export_format, export_format)

def export_raster(input, output, **opts):
    epsg = opts.get('epsg')
    expression = opts.get('expression')
    export_format = opts.get('format')
    rescale = opts.get('rescale')
    color_map = opts.get('color_map')
    hillshade = opts.get('hillshade')
    asset_type = opts.get('asset_type')
    name = opts.get('name', 'raster') # KMZ specific
    crop_wkt = opts.get('crop')

    dem = asset_type in ['dsm', 'dtm']

    if crop_wkt is not None:
        with rasterio.open(input) as ds:
            crop = GEOSGeometry(crop_wkt)
            crop.srid = 4326
            cutline, bounds = geom_transform_wkt_bbox(crop, ds, 'raster')
            vrt_options = {'cutline': cutline, 'nodata': 0}
    else:
        vrt_options = None
    
    with COGReader(input, vrt_options=vrt_options) as ds_src:
        src = ds_src.dataset
        profile = src.meta.copy()
        win_bounds = None
        win_transform = None
        dst_width = src.width
        dst_height = src.height

        if vrt_options is None:
            reader = src
            win = Window(0, 0, dst_width, dst_height)
            win_transform = src.transform
        else:
            reader = WarpedVRT(src, **vrt_options)
            dst_width = bounds[2] - bounds[0]
            dst_height = bounds[3] - bounds[1]
            win = Window(bounds[0], bounds[1], dst_width, dst_height)
            win_bounds = window_bounds(win, profile['transform'])

            win_transform, width, height = calculate_default_transform(
                src.crs, src.crs, src.width, src.height,
                left=win_bounds[0],
                bottom=win_bounds[1],
                right=win_bounds[2],
                top=win_bounds[3],
                dst_width=dst_width,
                dst_height=dst_height)

            profile.update(
                transform=win_transform,
                width=width,
                height=height
            )
            
        # Output format
        driver = "GTiff"
        compress = None
        max_bands = 9999
        with_alpha = True
        rgb = False
        indexes = src.indexes
        output_raster = output
        jpg_background = 255 # white

        # KMZ is special, we just export it as GeoTIFF
        # and then call GDAL to tile/package it
        kmz = export_format == "kmz"
        if kmz:
            export_format = "gtiff-rgb"
            path_base, _ = os.path.splitext(output)
            output_raster = path_base + ".kmz.tif"

        if export_format == "jpg":
            driver = "JPEG"
            profile.update(quality=90)
            band_count = 3
            with_alpha = False
            rgb = True
        elif export_format == "png":
            driver = "PNG"
            band_count = 4
            rgb = True
        elif export_format == "gtiff-rgb":
            compress = "JPEG"
            profile.update(jpeg_quality=90)
            profile.update(BIGTIFF='IF_SAFER')
            band_count = 4
            rgb = True
        else:
            compress = "DEFLATE"
            profile.update(BIGTIFF='IF_SAFER')
            band_count = src.count

        if compress is not None:
            profile.update(compress=compress)
            profile.update(predictor=2 if compress == "DEFLATE" else 1)

        if rgb and rescale is None:
            # Compute min max
            nodata = None
            if asset_type == 'orthophoto':
                nodata = 0
            md = ds_src.metadata(pmin=2.0, pmax=98.0, hist_options={"bins": 255}, nodata=nodata, vrt_options=vrt_options)
            rescale = [md['statistics']['1']['min'], md['statistics']['1']['max']]

        ci = src.colorinterp

        if rgb and expression is None:
            # More than 4 bands?
            if len(ci) > 4:
                # Try to find RGBA band order
                if ColorInterp.red in ci and \
                        ColorInterp.green in ci and \
                        ColorInterp.blue in ci and \
                        ColorInterp.alpha in ci:
                    indexes = (ci.index(ColorInterp.red) + 1,
                                ci.index(ColorInterp.green) + 1,
                                ci.index(ColorInterp.blue) + 1,
                                ci.index(ColorInterp.alpha) + 1)

        if ColorInterp.alpha in ci:
            mask = reader.read(ci.index(ColorInterp.alpha) + 1, window=win)
        else:
            mask = reader.dataset_mask(window=win)

        cmap = None
        if color_map:
            try:
                cmap = colormap.get(color_map)
            except InvalidColorMapName:
                logger.warning("Invalid colormap {}".format(color_map))


        def process(arr, skip_rescale=False, skip_alpha=False, skip_type=False):
            if not skip_rescale and rescale is not None:
                arr = linear_rescale(arr, in_range=rescale)
            if not skip_alpha and not with_alpha:
                arr[mask==0] = jpg_background
            if not skip_type and rgb and arr.dtype != np.uint8:
                arr = arr.astype(np.uint8)

            return arr

        def update_rgb_colorinterp(dst):
            if with_alpha:
                dst.colorinterp = [ColorInterp.red, ColorInterp.green, ColorInterp.blue, ColorInterp.alpha]
            else:
                dst.colorinterp = [ColorInterp.red, ColorInterp.green, ColorInterp.blue]

        profile.update(driver=driver, count=band_count)
        if rgb:
            profile.update(dtype=rasterio.uint8)

        if dem and rgb and profile.get('nodata') is not None:
            profile.update(nodata=None)

        # Define write band function
        # Reprojection needed?
        if src.crs is not None and epsg is not None and src.crs.to_epsg() != epsg:
            dst_crs = "EPSG:{}".format(epsg)

            if win_bounds is not None:
                transform, width, height = calculate_default_transform(
                    src.crs, dst_crs, src.width, src.height,
                    left=win_bounds[0],
                    bottom=win_bounds[1],
                    right=win_bounds[2],
                    top=win_bounds[3],
                    dst_width=dst_width,
                    dst_height=dst_height)
            else:
                transform, width, height = calculate_default_transform(
                    src.crs, dst_crs, src.width, src.height, *src.bounds)

            profile.update(
                crs=dst_crs,
                transform=transform,
                width=width,
                height=height
            )

            def write_band(arr, dst, band_num):
                reproject(source=arr, 
                        destination=rasterio.band(dst, band_num),
                        src_transform=win_transform,
                        src_crs=src.crs,
                        dst_transform=transform,
                        dst_crs=dst_crs,
                        resampling=Resampling.nearest,
                        num_threads=4)

        else:
            # No reprojection needed
            def write_band(arr, dst, band_num):
                dst.write(arr, band_num)

        if expression is not None:
            # Apply band math
            if rgb:
                profile.update(dtype=rasterio.uint8, count=band_count)
            else:
                profile.update(dtype=rasterio.float32, count=1, nodata=-9999)

            bands_names = ["b{}".format(b) for b in tuple(sorted(set(re.findall(r"b(?P<bands>[0-9]{1,2})", expression))))]
            rgb_expr = expression.split(",")
            indexes = tuple([int(b.replace("b", "")) for b in bands_names])

            alpha_index = None
            if has_alpha_band(src):
                try:
                    alpha_index = src.colorinterp.index(ColorInterp.alpha) + 1
                    indexes += (alpha_index, )
                except ValueError:
                    pass

            data = reader.read(indexes=indexes, window=win, out_dtype=np.float32)
            arr = dict(zip(bands_names, data))
            arr = np.array([np.nan_to_num(ne.evaluate(bloc.strip(), local_dict=arr)) for bloc in rgb_expr])

            # Set nodata values
            index_band = arr[0]
            if alpha_index is not None:
                # -1 is the last band = alpha
                index_band[data[-1] == 0] = -9999

            # Remove infinity values
            index_band[index_band>1e+30] = -9999
            index_band[index_band<-1e+30] = -9999

            # Make sure this is float32
            arr = arr.astype(np.float32)

            with rasterio.open(output_raster, 'w', **profile) as dst:
                # Apply colormap?
                if rgb and cmap is not None:
                    rgb_data, _ = apply_cmap(process(arr, skip_alpha=True), cmap)

                    band_num = 1
                    for b in rgb_data:
                        write_band(process(b, skip_rescale=True), dst, band_num)
                        band_num += 1

                    if with_alpha:
                        write_band(mask, dst, band_num)
                    
                    update_rgb_colorinterp(dst)
                else:
                    # Raw
                    write_band(process(arr)[0], dst, 1)
        elif dem:
            # Apply hillshading, colormaps to elevation
            with rasterio.open(output_raster, 'w', **profile) as dst:
                arr = reader.read(window=win)

                intensity = None
                if hillshade is not None and hillshade > 0:
                    delta_scale = ZOOM_EXTRA_LEVELS ** 2
                    dx = src.meta["transform"][0] * delta_scale
                    dy = src.meta["transform"][4] * delta_scale
                    ls = LightSource(azdeg=315, altdeg=45)
                    intensity = ls.hillshade(arr[0], dx=dx, dy=dy, vert_exag=hillshade)
                    intensity = intensity * 255.0

                # Apply colormap?
                if rgb and cmap is not None:
                    rgb_data, _ = apply_cmap(process(arr, skip_alpha=True), cmap)
                    arr = None

                    if intensity is not None:
                        rgb_data = hsv_blend(rgb_data, intensity)
                        
                    band_num = 1
                    for b in rgb_data:
                        write_band(process(b, skip_rescale=True), dst, band_num)
                        band_num += 1
                    
                    if with_alpha:
                        write_band(mask, dst, band_num)

                    update_rgb_colorinterp(dst)
                else:
                    # Raw
                    write_band(process(arr)[0], dst, 1)
        else:
            # Copy bands as-is
            with rasterio.open(output_raster, 'w', **profile) as dst:
                band_num = 1
                for idx in indexes:
                    ci = src.colorinterp[idx - 1]
                    arr = reader.read(idx, window=win)

                    if ci == ColorInterp.alpha:
                        if with_alpha:
                            write_band(arr, dst, band_num)
                            band_num += 1
                    else:
                        write_band(process(arr), dst, band_num)
                        band_num += 1
                
                new_ci = [src.colorinterp[idx - 1] for idx in indexes]
                if not with_alpha:
                    new_ci = [ci for ci in new_ci if ci != ColorInterp.alpha]
                    
                dst.colorinterp = new_ci
        
        # Close warped vrt
        if vrt_options is not None:
            reader.close()
        
        if kmz:
            subprocess.check_output(["gdal_translate", "-of", "KMLSUPEROVERLAY", 
                                        "-co", "Name={}".format(name),
                                        "-co", "FORMAT=AUTO", output_raster, output])
