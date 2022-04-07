##############################################################################
# Copyright by The HDF Group.                                                #
# All rights reserved.                                                       #
#                                                                            #
# This file is part of HSDS (HDF5 Scalable Data Service), Libraries and      #
# Utilities.  The full HSDS copyright notice, including                      #
# terms governing use, modification, and redistribution, is contained in     #
# the file COPYING, which can be found at the root of the source code        #
# distribution tree.  If you do not have access to this file, you may        #
# request a copy from help@hdfgroup.org.                                     #
##############################################################################
#
# value operations
# handles dataset /value requests
#
import asyncio
import time
from multiprocessing import shared_memory
from asyncio import CancelledError
import base64
import numpy as np
from aiohttp.web_exceptions import HTTPException, HTTPBadRequest, HTTPNotFound
from aiohttp.web_exceptions import HTTPRequestEntityTooLarge
from aiohttp.web_exceptions import HTTPConflict, HTTPInternalServerError
from aiohttp.web_exceptions import HTTPServiceUnavailable
from aiohttp.client_exceptions import ClientError
from aiohttp.web import StreamResponse

from .util.httpUtil import getHref, getAcceptType, getContentType, http_get, http_put
from .util.httpUtil import http_post, request_read, jsonResponse
from .util.idUtil import isValidUuid, getDataNodeUrl, getNodeCount
from .util.domainUtil import getDomainFromRequest, isValidDomain
from .util.domainUtil import getBucketForDomain
from .util.hdf5dtype import getItemSize, createDataType
from .util.dsetUtil import getSelectionList, getSliceQueryParam, isNullSpace  
from .util.dsetUtil import getFillValue, isExtensible
from .util.dsetUtil import getSelectionShape, getDsetMaxDims, getChunkLayout 
from .util.chunkUtil import getNumChunks, getChunkIds, getChunkId
from .util.chunkUtil import getChunkIndex, getChunkSuffix, checkQuery
from .util.chunkUtil import getChunkCoverage, getDataCoverage
from .util.chunkUtil import getChunkIdForPartition, getQueryDtype
from .util.arrayUtil import bytesArrayToList, jsonToArray, getShapeDims
from .util.arrayUtil import getNumElements, arrayToBytes, bytesToArray
from .util.arrayUtil import squeezeArray
from .util.authUtil import getUserPasswordFromRequest, validateUserPassword
from .servicenode_lib import getObjectJson, validateAction
from . import config
from . import hsds_logger as log

CHUNK_REF_LAYOUTS = ('H5D_CONTIGUOUS_REF',
                     'H5D_CHUNKED_REF',
                     'H5D_CHUNKED_REF_INDIRECT')


def get_hrefs(request, dset_json):
    """
    Convience function to set up hrefs for GET
    """
    hrefs = []
    dset_id = dset_json["id"]
    dset_uri = f"/datasets/{dset_id}"
    self_uri = f"{dset_uri}/value"
    hrefs.append({'rel': 'self', 'href': getHref(request, self_uri)})
    root_uri = '/groups/' + dset_json["root"]
    hrefs.append({'rel': 'root', 'href': getHref(request, root_uri)})
    hrefs.append({'rel': 'home', 'href': getHref(request, '/')})
    hrefs.append({'rel': 'owner', 'href': getHref(request, dset_uri)})
    return hrefs
    

async def get_slices(app, select, dset_json, bucket=None):
    """ Get desired slices from selection query param string or json value.
       If select is none or empty, slices for entire datashape will be 
       returned.
       Refretch dims if the dataset is extensible 
    """
    
    dset_id = dset_json['id']
    datashape = dset_json["shape"]
    if datashape["class"] == 'H5S_NULL':
        msg = "Null space datasets can not be used as target for GET value"
        log.warn(msg)
        raise HTTPBadRequest(reason=msg)
    
    dims = getShapeDims(datashape)  # throws 400 for HS_NULL dsets
    maxdims = getDsetMaxDims(dset_json)

    # refetch the dims if the dataset is extensible and request or hasn't
    # provided an explicit region
    if isExtensible(dims, maxdims) and (select is None or not select):
        kwargs = {"bucket": bucket, "refresh": True}
        dset_json = await getObjectJson(app, dset_id, **kwargs)
        dims = getShapeDims(dset_json["shape"])

    slices = None  # selection for read
    if isExtensible and select:
        try:
            slices = getSelectionList(select, dims)
        except ValueError:
            # exception might be due to us having stale version of dims,
            # so use refresh
            kwargs = {"bucket": bucket, "refresh": True}
            dset_json = await getObjectJson(app, dset_id, **kwargs)
            dims = getShapeDims(dset_json["shape"])
            slices = None  # retry below

    if slices is None:
        try:
            slices = getSelectionList(select, dims)
        except ValueError as ve:
            msg = str(ve)
            log.warn(msg)
            raise HTTPBadRequest(reason=msg)
    return slices


async def write_chunk_hyperslab(app, chunk_id, dset_json, slices, arr,
                                bucket=None):
    """ write the chunk selection to the DN
    chunk_id: id of chunk to write to
    chunk_sel: chunk-relative selection to write to
    np_arr: numpy array of data to be written
    """

    if not bucket:
        bucket = config.get("bucket_name")

    msg = f"write_chunk_hyperslab, chunk_id:{chunk_id}, slices:{slices}, "
    msg += f"bucket: {bucket}"
    log.info(msg)
    if "layout" not in dset_json:
        log.error(f"No layout found in dset_json: {dset_json}")
        raise HTTPInternalServerError()
    partition_chunk_id = getChunkIdForPartition(chunk_id, dset_json)
    if partition_chunk_id != chunk_id:
        log.debug(f"using partition_chunk_id: {partition_chunk_id}")
        chunk_id = partition_chunk_id  # replace the chunk_id

    if "type" not in dset_json:
        log.error(f"No type found in dset_json: {dset_json}")
        raise HTTPInternalServerError()

    layout = getChunkLayout(dset_json)
    chunk_sel = getChunkCoverage(chunk_id, slices, layout)
    log.debug(f"chunk_sel: {chunk_sel}")
    data_sel = getDataCoverage(chunk_id, slices, layout)
    log.debug(f"data_sel: {data_sel}")
    log.debug(f"arr.shape: {arr.shape}")
    arr_chunk = arr[data_sel]
    req = getDataNodeUrl(app, chunk_id)
    req += "/chunks/" + chunk_id

    log.debug(f"PUT chunk req: {req}")
    data = arrayToBytes(arr_chunk)
    # pass itemsize, type, dimensions, and selection as query params
    params = {}
    select = getSliceQueryParam(chunk_sel)
    params["select"] = select
    if bucket:
        params["bucket"] = bucket

    try:
        json_rsp = await http_put(app, req, data=data, params=params)
        msg = f"got rsp: {json_rsp} for put binary request: {req}, "
        msg += f"{len(data)} bytes"
        log.debug(msg)
    except ClientError as ce:
        log.error(f"Error for http_put({req}): {ce} ")
        raise HTTPInternalServerError()
    except CancelledError as cle:
        log.warn(f"CancelledError for http_put({req}): {cle}")


async def read_chunk_hyperslab(app, chunk_id, dset_json, np_arr,
                               query=None, query_update=None, limit=0,
                               chunk_map=None, bucket=None):
    """ read the chunk selection from the DN
    chunk_id: id of chunk to write to
    chunk_sel: chunk-relative selection to read from
    np_arr: numpy array to store read bytes
    chunk_map: map of chunk_id to chunk_offset and chunk_size
        chunk_offset: location of chunk with the s3 object
        chunk_size: size of chunk within the s3 object (or 0 if the
           entire object)
    bucket: s3 bucket to read from
    """
    if not bucket:
        bucket = config.get("bucket_name")

    if chunk_map is None:
        log.error("expected chunk_map to be set")
        return

    msg = f"read_chunk_hyperslab, chunk_id: {chunk_id},"
    """
    msg += " slices: ["
    for s in slices:
        if isinstance(s, slice):
            msg += f"{s},"
        else:
            if len(s) > 5:
                # avoid large output lines
                msg += f"[{s[0]}, {s[1]}, ..., {s[-2]}, {s[-1]}],"
            else:
                msg += f"{s},"
    """
    msg += f" bucket: {bucket}"
    if query is not None:
        msg += f" query: {query} limit: {limit}"
    log.info(msg)
    if chunk_id not in chunk_map:
        log.warn(f"expected to find {chunk_id} in chunk_map")
        return
    chunk_info = chunk_map[chunk_id]
    log.debug(f"using chunk_map entry for {chunk_id}: {chunk_info}")

    partition_chunk_id = getChunkIdForPartition(chunk_id, dset_json)
    if partition_chunk_id != chunk_id:
        log.debug(f"using partition_chunk_id: {partition_chunk_id}")
        chunk_id = partition_chunk_id  # replace the chunk_id

    if "type" not in dset_json:
        log.error(f"No type found in dset_json: {dset_json}")
        raise HTTPInternalServerError()

    chunk_shape = None # expected return array shape
    chunk_sel = None  # for hyperslab
    data_sel = None   # for hyperslab
    point_list = None     # for point sel
    point_index = None    # for point sel
    select = None       # select query string
    method = 'GET'      # default http method
    # for hyperslab selections, chunk_sel and data_sel keys are used
    if 'chunk_sel' in chunk_info:
        chunk_sel = chunk_info['chunk_sel']
        log.debug(f"read_chunk_hyperslab - chunk_sel: {chunk_sel}")
        select = getSliceQueryParam(chunk_sel)
    
    if 'data_sel' in chunk_info:
        data_sel = chunk_info['data_sel']
        log.debug(f"read_chunk_hyperslab - data_sel: {data_sel}")
        chunk_shape = getSelectionShape(chunk_sel)
        log.debug(f"hyperslab selection - chunk_shape: {chunk_shape}")
    
    if 'points' in chunk_info:
        point_list = chunk_info['points']
        if 'indices' not in chunk_info:
            log.error(f"expected to find 'indices' in item: {chunk_info}")
            raise HTTPInternalServerError()
        point_index = chunk_info['indices']
        method = 'POST'
        chunk_shape = [len(point_list),]
        log.debug(f"point selection - chunk_shape: {chunk_shape}")
    
    type_json = dset_json["type"]
    dt = createDataType(type_json) 
    if query is None and query_update is None:
        query_dtype = None
    else:
        query_dtype = getQueryDtype(dt)

    chunk_arr = None
    array_data = None

    # pass dset json and selection as query params
    params = {}
    # params["select"] = select
    if 's3path' in chunk_info:
        params['s3path'] = chunk_info['s3path']
    if 's3offset' in chunk_info:
        params['s3offset'] = chunk_info['s3offset']
    if 's3size' in chunk_info:
        params['s3size'] = chunk_info['s3size']

    # set query-based params
    if query is not None:
        params['query'] = query
        if limit > 0:
            params['Limit'] = limit
            
    # bucket will be used to get dset json even when s3path is used for 
    # the chunk data
    params["bucket"] = bucket

    if point_list is not None:
        # set query params for point selection
        log.debug(f"read_chunk_hyperslab - point selection {len(point_list)} points")
        params['action'] = 'get'
        params['count'] = len(point_list)
        method = 'POST'
    elif query_update is not None:
        method = 'PUT'

    req = getDataNodeUrl(app, chunk_id)
    req += "/chunks/" + chunk_id
    
    if select is not None:
        # use post if the select param is long
        max_select_len = config.get("http_max_url_length", default=512)
        max_select_len //= 2 # use up to half the alloted url length for select
        if len(select) > max_select_len:
            method = 'POST'

    body = None
    if method == 'POST':
        if point_list is not None:
            num_points = len(point_list)
            log.debug(f"read_point_sel: {num_points}")
            point_dt = np.dtype('u8')  # use unsigned long for point index
            np_arr_points = np.asarray(point_list, dtype=point_dt)
            body = np_arr_points.tobytes()
        elif select is not None:
            body = {'select': select}
        else:
            log.error("read_chunk_hyperslab - expected hyperslab or point selection")
            raise HTTPInternalServerError()
    elif method == 'PUT':
        # query update
        body = query_update
    else:
        if select is not None:
            params['select'] = select

    # send request
    try:
        log.debug(f"read_chunk_hyperslab - {method} chunk req: {req}")
        log.debug(f"params: {params}")
        if method == 'GET':
            array_data = await http_get(app, req, params=params)
            log.debug(f"http_get {req}, returned {len(array_data)} bytes")
        elif method == 'PUT':
            array_data = await http_put(app, req, data=body, params=params)
            log.debug(f"http_put {req}, returned {len(array_data)} bytes")
        else:  # POST
            array_data = await http_post(app, req, data=body, params=params)
            log.debug(f"http_post {req}, returned {len(array_data)} bytes")
    except HTTPNotFound:
        if query is None and "s3path" in params:
            s3path = params["s3path"]
            # external HDF5 file, should exist
            log.warn(f"s3path: {s3path} for S3 range get not found")
            raise         

    # process response            
    if array_data is None:
        log.debug(f"No data returned for chunk: {chunk_id}")
    else:
        log.debug(f"got data for chunk: {chunk_id}")
        log.debug(f"data: {len(array_data)} bytes")
        if query is not None or query_update is not None:
            # TBD: this needs to be fixed up for variable length dtypes
            nrows = len(array_data) // query_dtype.itemsize
            try:
                chunk_arr = bytesToArray(array_data, query_dtype, [nrows,])
            except ValueError as ve:
                log.warn(f"bytesToArray ValueError: {ve}")
                raise HTTPBadRequest()
            # save result to chunk_info 
            # chunk results will be merged later
            chunk_info["query_rsp"] = chunk_arr
        else:
            # convert binary data to numpy array
            try:
                chunk_arr = bytesToArray(array_data, dt, chunk_shape)
            except ValueError as ve:
                log.warn(f"bytesToArray ValueError: {ve}")
                raise HTTPBadRequest()
            nelements_read = getNumElements(chunk_arr.shape)
            nelements_expected = getNumElements(chunk_shape)
            if nelements_read != nelements_expected:
                msg = f"Expected {nelements_expected} points, "
                msg += f"but got: {nelements_read}"
                log.error(msg)
                raise HTTPInternalServerError()
            chunk_arr = chunk_arr.reshape(chunk_shape)

            log.info(f"chunk_arr shape: {chunk_arr.shape}")
            log.info(f"data_sel: {data_sel}")
            log.info(f"np_arr shape: {np_arr.shape}")
        
            if point_list is not None:
                # point selection
                # Fill in the return array based on passed in index values
                np_arr[point_index] = chunk_arr
            else:
                # hyperslab selection
                np_arr[data_sel] = chunk_arr
    log.debug(f"read_chunk_hyperslab {chunk_id} - done")        


async def read_point_sel(app, chunk_id, dset_json, point_list, point_index,
                         np_arr, chunk_map=None, bucket=None):
    """
    Read point selection
    --
    app: application object
    chunk_id: id of chunk to read from
    dset_json: dset JSON
    point_list: array of points to read
    point_index: index of arr element to update for a given point
    arr: numpy array to store read bytes
    """

    if not bucket:
        bucket = config.get("bucket_name")

    msg = f"read_point_sel, chunk_id: {chunk_id}, bucket: {bucket}"
    log.info(msg)

    partition_chunk_id = getChunkIdForPartition(chunk_id, dset_json)
    if partition_chunk_id != chunk_id:
        log.debug(f"using partition_chunk_id: {partition_chunk_id}")
        chunk_id = partition_chunk_id  # replace the chunk_id

    point_dt = np.dtype('u8')  # use unsigned long for point index

    if "type" not in dset_json:
        log.error(f"No type found in dset_json: {dset_json}")
        raise HTTPInternalServerError()

    num_points = len(point_list)
    log.debug(f"read_point_sel: {num_points}")
    np_arr_points = np.asarray(point_list, dtype=point_dt)
    post_data = np_arr_points.tobytes()

    # set action as query params
    params = {}
    params["action"] = "get"
    params["count"] = num_points

    fill_value = getFillValue(dset_json)

    np_arr_rsp = None
    dt = np_arr.dtype

    def defaultArray():
        # no data, return zero array
        if fill_value:
            arr = np.empty((num_points,), dtype=dt)
            arr[...] = fill_value
        else:
            arr = np.zeros((num_points,), dtype=dt)
        return arr

    np_arr_rsp = None
    if chunk_map:
        if chunk_id not in chunk_map:
            msg = f"{chunk_id} not found in chunk_map, returning default arr"
            log.debug(msg)
            np_arr_rsp = defaultArray()
        else:
            chunk_info = chunk_map[chunk_id]
            params["s3path"] = chunk_info["s3path"]
            params["s3offset"] = chunk_info["s3offset"]
            params["s3size"] = chunk_info["s3size"]
   
    # bucket will be used to get dset json even when s3path is used for 
    # the chunk data
    params["bucket"] = bucket

    if np_arr_rsp is None:
        # make request to DN node
        req = getDataNodeUrl(app, chunk_id)
        req += "/chunks/" + chunk_id
        log.debug(f"GET chunk req: {req}")
        try:
            kwargs = {"params": params, "data": post_data}
            rsp_data = await http_post(app, req, **kwargs)
            msg = f"got rsp for http_post({req}): {len(rsp_data)} bytes"
            log.debug(msg)
            np_arr_rsp = bytesToArray(rsp_data, dt, (num_points,))
        except HTTPNotFound:
            if "s3path" in params:
                s3path = params["s3path"]
                # external HDF5 file, should exist
                log.warn(f"s3path: {s3path} for S3 range get found")
                raise
            # no data, return zero array
            np_arr_rsp = defaultArray()

    npoints_read = len(np_arr_rsp)
    log.info(f"got {npoints_read} points response")

    if npoints_read != num_points:
        msg = f"Expected {num_points} points, but got: {npoints_read}"
        log.error(msg)
        raise HTTPInternalServerError()

    # Fill in the return array based on passed in index values
    for i in range(num_points):
        index = point_index[i]
        np_arr[index] = np_arr_rsp[i]


async def write_point_sel(app, chunk_id, dset_json, point_list, point_data,
                          bucket=None):
    """
    Write point selection
    --
      app: application object
      chunk_id: id of chunk to write to
      dset_json: dset JSON
      point_list: array of points to write
      point_data: index of arr element to update for a given point
    """

    if not bucket:
        bucket = config.get("bucket_name")

    msg = f"write_point_sel, chunk_id: {chunk_id}, points: {point_list}, "
    msg += f"data: {point_data}"
    log.info(msg)
    if "type" not in dset_json:
        log.error(f"No type found in dset_json: {dset_json}")
        raise HTTPInternalServerError()

    datashape = dset_json["shape"]
    dims = getShapeDims(datashape)
    rank = len(dims)
    type_json = dset_json["type"]
    dset_dtype = createDataType(type_json)  # np datatype

    partition_chunk_id = getChunkIdForPartition(chunk_id, dset_json)
    if partition_chunk_id != chunk_id:
        log.debug(f"using partition_chunk_id: {partition_chunk_id}")
        chunk_id = partition_chunk_id  # replace the chunk_id

    req = getDataNodeUrl(app, chunk_id)
    req += "/chunks/" + chunk_id
    log.debug("POST chunk req: " + req)

    num_points = len(point_list)
    log.debug(f"write_point_sel - {num_points}")

    # create a numpy array with point_data
    data_arr = jsonToArray((num_points,), dset_dtype, point_data)

    # create a numpy array with the following type:
    #   (coord1, coord2, ...) | dset_dtype
    if rank == 1:
        coord_type_str = "uint64"
    else:
        coord_type_str = f"({rank},)uint64"
    type_fields = [("coord", np.dtype(coord_type_str)), ("value", dset_dtype)]
    comp_type = np.dtype(type_fields)
    np_arr = np.zeros((num_points, ), dtype=comp_type)

    # Zip together coordinate and point_data to one numpy array
    for i in range(num_points):
        if rank == 1:
            elem = (point_list[i], data_arr[i])
        else:
            elem = (tuple(point_list[i]), data_arr[i])
        np_arr[i] = elem

    # TBD - support VLEN data
    post_data = np_arr.tobytes()

    # pass dset_json as query params
    params = {}
    params["action"] = "put"
    params["count"] = num_points
    params["bucket"] = bucket

    json_rsp = await http_post(app, req, params=params, data=post_data)
    log.debug(f"post to {req} returned {json_rsp}")


async def read_chunk_query(app, chunk_id, dset_json, slices, query, rsp_dict,
                           query_update=None, limit=0, chunk_map=None, bucket=None):
    """ read the chunk selection from the DN
    chunk_id: id of chunk to write to
    chunk_sel: chunk-relative selection to read from
    np_arr: numpy array to store read bytes
    """
    msg = f"read_chunk_query, chunk_id: {chunk_id}, slices: {slices}, "
    msg += f"query: {query} limit: {limit}"
    if query_update:
        msg += f", query_update: {query_update}"
    log.info(msg)
    chunk_rsp = None
    max_retries = config.get("dn_max_retires", default=3)

    partition_chunk_id = getChunkIdForPartition(chunk_id, dset_json)
    if partition_chunk_id != chunk_id:
        log.debug(f"using partition_chunk_id: {partition_chunk_id}")
        chunk_id = partition_chunk_id  # replace the chunk_id

    layout = getChunkLayout(dset_json)
    chunk_sel = getChunkCoverage(chunk_id, slices, layout)

    # pass query as param
    params = {}
    params["query"] = query
    if limit > 0:
        params["Limit"] = limit
    if chunk_map:
        if chunk_id not in chunk_map:
            # no data, don't return any results
            chunk_rsp = None
        else:
            chunk_info = chunk_map[chunk_id]
            params["s3path"] = chunk_info["s3path"]
            params["s3offset"] = chunk_info["s3offset"]
            params["s3size"] = chunk_info["s3size"]
  
    # bucket will be used to get dset json even when s3path is used for 
    # the chunk data
    params["bucket"] = bucket

    chunk_shape = getSelectionShape(chunk_sel)
    log.debug(f"chunk_shape: {chunk_shape}")
    select = getSliceQueryParam(chunk_sel)
    params["select"] = select
 
    req = getDataNodeUrl(app, chunk_id)
    req += "/chunks/" + chunk_id
    retry = 0
    status = None
    while retry < max_retries:
        if retry > 0:
            sleep_time = 1
            log.warn(f"read_chunk_query - sleeping for {sleep_time} before retrying request")
            time.sleep(sleep_time)
        try:
            if query_update:
                log.debug(f"PUT chunk req: {req}, data: {query_update}")
                chunk_rsp = await http_put(app, req, data=query_update, params=params)
            else:
                log.debug(f"GET chunk req: {req}")
                chunk_rsp = await http_get(app, req, params=params)
                log.debug(f"got {len(chunk_rsp)} bytes from query: {query} on chunk: {chunk_id}")
            rsp_dict[chunk_id] = chunk_rsp
            status = 200
        except HTTPNotFound:
            # no data, don't return any results
            log.debug(f"no results from query: {query} on chunk: {chunk_id}")
            rsp_dict[chunk_id] = None  
            status = 404
        except ClientError as ce:
            log.error(f"ClientError {type(ce)} for read_chunk_query({chunk_id}): {ce} ")
            status = 500
        except CancelledError as cle:
            log.warn(f"CancelledError for read_chunk_query({chunk_id}): {cle}")
            status = 500
        except HTTPBadRequest as hbr:
            rsp_dict[chunk_id] = 400
            log.error(f"HTTPBadRequest for read_chunk_query({chunk_id}): {hbr} ")
        except HTTPInternalServerError as ise:
            status = 500
            log.error(f"HTTPInternalServerError for read_chunk_query({chunk_id}): {ise} ")
        except Exception as e:
            status = 500
            log.error(f"Unexpected exception {type(e)} for read_chunk_query({chunk_id}): {e} ")
        if chunk_id in rsp_dict:
            # got a result, break from retry loop
            break
        retry += 1
    if chunk_id not in rsp_dict:
        log.error(f"read_chunk_query - max retries exceeded for chunk: {chunk_id}")
        # return the status code
        rsp_dict[chunk_id] = status


async def getChunkLocations(app, dset_id, dset_json, chunkinfo_map, chunk_ids, bucket=None):
    """
    Get info for chunk locations (for reference layouts)
    """
    layout = dset_json["layout"]

    if layout["class"] not in CHUNK_REF_LAYOUTS:
        msg = f"skip getChunkLocations for layout class: { layout['class'] }"
        log.debug(msg)
        return

    datashape = dset_json["shape"]
    datatype = dset_json["type"]
    if datashape["class"] == 'H5S_NULL':
        log.error("H5S_NULL shape class used with reference chunk layout")
        raise HTTPInternalServerError()
    dims = getShapeDims(datashape)
    rank = len(dims)
    #chunk_ids = list(chunkinfo_map.keys())
    #chunk_ids.sort()
    num_chunks = len(chunk_ids)
    msg = f"getChunkLocations for dset: {dset_id} bucket: {bucket} "
    msg += f"rank: {rank} num chunk_ids: {num_chunks}"
    log.info(msg)
    log.debug(f"getChunkLocations layout: {layout}")

    def getChunkItem(chunkid):
        if chunk_id in chunkinfo_map:
            chunk_item = chunkinfo_map[chunk_id]
        else:
            chunk_item = {}
            chunkinfo_map[chunk_id] = chunk_item
        return chunk_item


    if layout["class"] == 'H5D_CONTIGUOUS_REF':
        s3path = layout["file_uri"]
        s3size = layout["size"]
        if s3size == 0:
            msg = "getChunkLocations - H5D_CONTIGUOUS_REF layout size 0, "
            msg += "no allocation"
            log.info(msg)
            return 
        chunk_dims = layout["dims"]
        item_size = getItemSize(datatype)
        chunk_size = item_size
        for dim in chunk_dims:
            chunk_size *= dim
        log.debug(f"using chunk_size: {chunk_size} for H5D_CONTIGUOUS_REF")

        for chunk_id in chunk_ids:
            log.debug(f"getChunkLocations - getting data for chunk: {chunk_id}")
            chunk_item = getChunkItem(chunk_id)
            chunk_index = getChunkIndex(chunk_id)
            if len(chunk_index) != rank:
                log.error("Unexpected chunk_index")
                raise HTTPInternalServerError()
            extent = item_size
            if "offset" not in layout:
                msg = "getChunkLocations - expected to find offset in chunk "
                msg += "layout for H5D_CONTIGUOUS_REF"
                log.error(msg)
                continue
            s3offset = layout["offset"]
            if not isinstance(s3offset, int):
                msg = "getChunkLocations - expected offset to be an int but "
                msg += f"got: {s3offset}"
                log.error(msg)
                continue
            log.debug(f"getChunkLocations s3offset: {s3offset}")
            for i in range(rank):
                dim = rank - i - 1
                index = chunk_index[dim]
                s3offset += index * chunk_dims[dim] * extent
                extent *= dims[dim]
            msg = f"setting chunk_info_map to s3offset: {s3offset} "
            msg == f"s3size: {s3size} for chunk_id: {chunk_id}"
            log.debug(msg)
            if s3offset > layout["offset"] + layout["size"]:
                msg = f"range get of s3offset: {s3offset} s3size: {s3size} "
                msg += "extends beyond end of contiguous dataset for "
                msg += f"chunk_id: {chunk_id}"
                log.warn(msg)
            chunk_item['s3path'] = s3path
            chunk_item['s3offset'] = s3offset
            chunk_item['s3size'] = chunk_size
    elif layout["class"] == 'H5D_CHUNKED_REF':
        s3path = layout["file_uri"]
        chunks = layout["chunks"]

        for chunk_id in chunk_ids:
            chunk_item = getChunkItem(chunk_id)
            s3offset = 0
            s3size = 0
            chunk_key = getChunkSuffix(chunk_id)
            if chunk_key in chunks:
                item = chunks[chunk_key]
                s3offset = item[0]
                s3size = item[1]
            chunk_item['s3path'] = s3path
            chunk_item['s3offset'] = s3offset
            chunk_item['s3size'] = s3size
             
    elif layout["class"] == 'H5D_CHUNKED_REF_INDIRECT':
        if "chunk_table" not in layout:
            log.error("Expected to find chunk_table in dataset layout")
            raise HTTPInternalServerError()
        chunktable_id = layout["chunk_table"]
        # get  state for dataset from DN.
        kwargs = {"bucket": bucket, "refresh": False}
        chunktable_json = await getObjectJson(app, chunktable_id, **kwargs)
        #log.debug(f"chunktable_json: {chunktable_json}")
        chunktable_dims = getShapeDims(chunktable_json["shape"])
        # TBD: verify chunktable type

        if len(chunktable_dims) != rank:
            msg = "Rank of chunktable should be same as the dataset"
            log.warn(msg)
            raise HTTPBadRequest(reason=msg)

        # convert the list of chunk_ids into a set of points to query in
        # the chunk table
        if rank == 1:
            arr_points = np.zeros((num_chunks,), dtype=np.dtype('u8'))
        else:
            arr_points = np.zeros((num_chunks, rank), dtype=np.dtype('u8'))
        for i in range(num_chunks):
            chunk_id = chunk_ids[i]
            log.debug(f"chunk_id for chunktable: {chunk_id}")
            indx = getChunkIndex(chunk_id)
            log.debug(f"get chunk indx: {indx}")
            if rank == 1:
                log.debug(f"convert: {indx[0]} to {indx}")
                indx = indx[0]
            arr_points[i] = indx
        msg = f"got chunktable points: {arr_points}, calling getSelectionData"
        log.debug(msg)
        point_data = await getSelectionData(app, 
                                            chunktable_id, 
                                            chunktable_json,
                                            points=arr_points,
                                            bucket=bucket)

        log.debug(f"got chunktable data: {point_data}")
        if "file_uri" in layout:
            s3_layout_path = layout["file_uri"]
        else:
            s3_layout_path = None

        for i in range(num_chunks):
            chunk_id = chunk_ids[i]
            item = point_data[i]
            s3offset = int(item[0])
            s3size = int(item[1])
            if s3_layout_path is None:
                if len(item) < 3:
                    msg = "expected chunk table to have three fields"
                    log.warn(msg)
                    raise HTTPBadRequest(reason=msg)
                e = item[2]
                if e:
                    s3path = e.decode('utf-8')
                    log.debug(f"got s3path: {s3path}")
            else:
                s3path = s3_layout_path
            chunk_item = getChunkItem(chunk_id)
            chunk_item['s3path'] = s3path
            chunk_item['s3offset'] = s3offset
            chunk_item['s3size'] = s3size
             
    else:
        log.error(f"Unexpected chunk layout: {layout['class']}")
        raise HTTPInternalServerError()

    log.debug(f"returning chunkinfo_map: {chunkinfo_map}")
    return chunkinfo_map

def get_chunk_selections(chunk_map, chunk_ids, slices, dset_json):
    """ Update chunk_map with chunk and data selections for the 
        given set of slices 
    """
    log.debug(f"get_chunk_selections - chunk_ids: {chunk_ids}")
    if not slices:
        log.debug("no slices set, returning")
        return # nothing to do
    log.debug(f"slices: {slices}")
    layout = getChunkLayout(dset_json)
    for chunk_id in chunk_ids:
        if chunk_id in chunk_map:
            item = chunk_map[chunk_id]
        else:
            item = {}
            chunk_map[chunk_id] = item

        chunk_sel = getChunkCoverage(chunk_id, slices, layout)
        log.debug(f"get_chunk_selections - chunk_id: {chunk_id}, chunk_sel: {chunk_sel}")
        item["chunk_sel"] = chunk_sel
        data_sel = getDataCoverage(chunk_id, slices, layout)
        log.debug(f"get_chunk_selections - data_sel: {data_sel}")
        item["data_sel"] = data_sel
     

async def write_chunk_query(app, chunk_id, dset_json, slices, query,
                            query_update, limit, bucket=None):
    """ update the chunk selection from the DN based on query string
    chunk_id: id of chunk to write to
    chunk_sel: chunk-relative selection to read from
    np_arr: numpy array to store read bytes
    """
    # TBD = see if this code can be merged with the read_chunk_query function
    msg = f"write_chunk_query, chunk_id: {chunk_id}, slices: {slices}, "
    msg += f"query: {query}, query_update: {query_update}"
    log.info(msg)
    partition_chunk_id = getChunkIdForPartition(chunk_id, dset_json)
    if partition_chunk_id != chunk_id:
        log.debug(f"using partition_chunk_id: {partition_chunk_id}")
        chunk_id = partition_chunk_id  # replace the chunk_id

    req = getDataNodeUrl(app, chunk_id)
    req += "/chunks/" + chunk_id
    log.debug("PUT chunk req: " + req)

    layout = getChunkLayout(dset_json)
    chunk_sel = getChunkCoverage(chunk_id, slices, layout)

    # pass query as param
    params = {}
    params["query"] = query
    if limit > 0:
        params["Limit"] = limit
    if bucket:
        params["bucket"] = bucket

    chunk_shape = getSelectionShape(chunk_sel)
    log.debug(f"chunk_shape: {chunk_shape}")
    select = getSliceQueryParam(chunk_sel)
    params["select"] = select
    try:
        dn_rsp = await http_put(app, req, data=query_update, params=params)
    except HTTPNotFound:
        # no data, don't return any results
        dn_rsp = {"index": [], "value": []}

    return dn_rsp


async def doPutQuery(request, query_update, dset_json):
    """
    Helper function for PUT queries
    """
    app = request.app
    params = request.rel_url.query
    if not isinstance(query_update, dict):
        msg = "Expected dict type for PUT query body, but "
        msg += f"got: {type(query_update)}"
        log.warn(msg)
        raise HTTPBadRequest(reason=msg)
    query = params["query"]
    domain = getDomainFromRequest(request)
    bucket = getBucketForDomain(domain)
     
    datashape = dset_json["shape"]
    dims = getShapeDims(datashape)
    num_rows = dims[0]
    log.debug(f"doPutQuery - num_rows: {num_rows}")

    type_json = dset_json["type"]
    log.debug(f"doPutQuery - type json: {type_json}")

    if type_json["class"] != 'H5T_COMPOUND':
        msg = "Expected compound type for PUT query operation"
        log.warn(msg)
        raise HTTPBadRequest(reason=msg)

    fields = type_json["fields"]
    field_names = set()
    for field in fields:
        field_names.add(field['name'])
    for key in query_update:
        if key not in field_names:
            msg = f"Unknown fieldname: {key} in update body"
            log.warn(msg)
            raise HTTPBadRequest(reason=msg)

    limit = 0
    if "Limit" in params:
        try:
            limit = int(params["Limit"])
        except ValueError:
            msg = "Invalid Limit query param"
            log.warn(msg)
            raise HTTPBadRequest(reason=msg)

    select = params.get("select")
    try:
        slices = await get_slices(app, select, dset_json, bucket=bucket)
    except ValueError as ve:
        msg = str(ve)
        log.warn(msg)
        raise HTTPBadRequest(reason=msg)
     
    msg = f"doPutQuery - got dim_slice: {slices[0]}"
    log.info(msg)

    layout = getChunkLayout(dset_json)

    num_chunks = getNumChunks(slices, layout)
    log.debug(f"doPutQuery - num_chunks: {num_chunks}")
    max_chunks = int(config.get('max_chunks_per_request'))
    if num_chunks > max_chunks:
        log.warn(f"doPutQuery - chunk count: {num_chunks}, greater than {max_chunks}")

    dset_id = dset_json["id"]

    try:
        chunk_ids = getChunkIds(dset_id, slices, layout)
    except ValueError:
        log.warn("doPutQuery - getChunkIds failed")
        raise HTTPInternalServerError()
    log.debug(f"doPutQuery - chunk_ids: {chunk_ids}")

    node_count = getNodeCount(app)
    if node_count == 0:
        log.warn("PutQuery request with no active dn nodes")
        raise HTTPServiceUnavailable()
    resp_index = []
    resp_value = []
    chunk_index = 0
    num_chunks = len(chunk_ids)
    count = 0

    while chunk_index < num_chunks:
        next_chunks = []
        for i in range(node_count):
            next_chunks.append(chunk_ids[chunk_index])
            chunk_index += 1
            if chunk_index >= num_chunks:
                break
        log.debug(f"doPutQuery - next chunk ids: {next_chunks}")
        # run query on DN nodes
        # do write_chunks sequentially do avoid exceeding the limit value
        for chunk_id in next_chunks:
            dn_rsp = await write_chunk_query(app,
                                             chunk_id,
                                             dset_json,
                                             slices,
                                             query,
                                             query_update,
                                             limit,
                                             bucket=bucket)
            log.debug(f"write_chunk_query: {dn_rsp}")
            num_hits = len(dn_rsp["index"])
            count += num_hits
            msg = f"doPutQuery - got {num_hits} for chunk_id: {chunk_id}, "
            msg += f"total: {count}"
            log.debug(msg)
            resp_index.extend(dn_rsp["index"])
            resp_value.extend(dn_rsp["value"])
            if limit > 0 and count >= limit:
                log.debug(f"doPutQuery - reached limit: {limit}")
                break
        if limit > 0 and count >= limit:
            # break out of outer loop
            break

    resp_json = {"index": resp_index, "value": resp_value}
    resp_json["hrefs"] = get_hrefs(request, dset_json)
    log.debug(f"doPutQuery - done returning {count} values")
    return resp_json

class ChunkCrawler:
    def __init__(self, app, chunk_ids, dset_json=None, chunk_map=None, 
                 bucket=None, slices=None, arr=None,
                 query=None, query_update=None, limit=0, points=None,
                 action=None):

        max_tasks_per_node = config.get("max_tasks_per_node_per_request", default=16)
        log.info(f"ChunkCrawler.__init__  {len(chunk_ids)} chunks, action={action}")
        log.debug(f"ChunkCrawler - chunk_ids: {chunk_ids}")

        self._app = app
        self._slices = slices
        self._chunk_ids = chunk_ids
        self._chunk_map = chunk_map
        self._dset_json = dset_json
        self._arr = arr
        self._points = points
        self._query = query
        self._query_update = query_update
        self._hits = 0
        self._limit = limit
        self._status_map = {}  # map of chunk_ids to status code
        self._q = asyncio.Queue()
        self._fail_count = 0
        self._action = action
        
        for chunk_id in chunk_ids:
            self._q.put_nowait(chunk_id)

        self._bucket = bucket
        max_tasks = max_tasks_per_node * getNodeCount(app)
        if len(chunk_ids) > max_tasks:
            self._max_tasks = max_tasks
        else:
            self._max_tasks = len(chunk_ids)

    def get_status(self):
        if len(self._status_map) != len(self._chunk_ids):
            msg = "get_status code while cralwer not complete"
            log.error(msg)
            raise ValueError(msg)
        for chunk_id in self._chunk_ids:
            if chunk_id not in self._status_map:
                msg = f"excpected to find chunk_id {chunk_id} in ChunkCrawler status_map"
                log.error(msg)
                raise KeyError(msg)
            chunk_status = self._status_map[chunk_id]
            if chunk_status not in (200, 201):
                log.info(f"returning chunk_status: {chunk_status} for chunk: {chunk_id}")
                return chunk_status
        
        return 200 # all good
            
    async def crawl(self):
        workers = [asyncio.Task(self.work())
                   for _ in range(self._max_tasks)]
        # When all work is done, exit.
        msg = f"ChunkCrawler max_tasks {self._max_tasks} = await queue.join "
        msg += f"- count: {len(self._chunk_ids)}"
        log.info(msg)
        await self._q.join()
        msg = f"ChunkCrawler - join complete - count: {len(self._chunk_ids)}"
        log.info(msg)

        for w in workers:
            w.cancel()
        log.debug("ChunkCrawler - workers canceled")

    async def work(self):
        """ Process chunk ids from queue till we are done"""
        while True:
            start = time.time()
            chunk_id = await self._q.get()
            if self._limit > 0 and self._hits >= self._limit:
                log.debug("ChunkCrawler - max hits exceeded, skipping fetch for chunk: {chunk_id}")
            else:
                await self.do_work(chunk_id)
                
            self._q.task_done()
            elapsed = time.time() - start
            msg = f"ChunkCrawler - task {chunk_id} start: {start:.3f} "
            msg += f"elapsed: {elapsed:.3f}"
            log.debug(msg)

    async def do_work(self, chunk_id):
        """ fetch the indicated chunk and update status map 
        """
        msg = f"ChunkCrawler - do_work for chunk: {chunk_id} bucket: "
        msg += f"{self._bucket}"
        log.debug(msg)
        max_retries = config.get("dn_max_retires", default=3)
        retry = 0
        status_code = None
        while retry < max_retries:
            try: 
                if self._action == "read_chunk_hyperslab":
                    await read_chunk_hyperslab(self._app,
                                chunk_id,
                                self._dset_json,
                                self._arr,
                                query=self._query,
                                query_update=self._query_update,
                                limit=self._limit,
                                chunk_map=self._chunk_map,
                                bucket=self._bucket)
                    log.debug(f"read_chunk_hyperslab - got 200 status for chunk_id: {chunk_id}")
                    status_code = 200
                elif self._action == "write_chunk_hyperslab":
                    await write_chunk_hyperslab(self._app,
                                chunk_id,
                                self._dset_json,
                                self._slices,
                                self._arr,
                                bucket=self._bucket)
                    log.debug(f"write_chunk_hyperslab - got 200 status for chunk_id: {chunk_id}")
                    status_code = 200
                elif self._action == "read_point_sel":
                    if not isinstance(self._points, dict):
                        log.error("ChunkCrawler - expected dict for points")
                        status_code = 500
                        break
                    if chunk_id not in self._points:
                        log.error(f"ChunkCrawler - read_point_sel, no entry for chunk: {chunk_id}")
                        status_code = 500
                        break
                    item = self._points[chunk_id]
                    point_list = item["indices"]
                    point_data = item["points"]

                    await read_point_sel(self._app, 
                                         chunk_id, 
                                         self._dset_json, 
                                         point_list, 
                                         point_data,
                                         self._arr,
                                         chunk_map=self._chunk_map,
                                         bucket=self._bucket)
                    log.debug(f"read_point_sel - got 200 status for chunk_id: {chunk_id}")
                    status_code = 200
                elif self._action == "write_point_sel":
                    if not isinstance(self._points, dict):
                        log.error("ChunkCrawler - expected dict for points")
                        status_code = 500
                        break
                    if chunk_id not in self._points:
                        log.error(f"ChunkCrawler - read_point_sel, no entry for chunk: {chunk_id}")
                        status_code = 500
                        break
                    item = self._points[chunk_id]
                    log.debug(f"item[{chunk_id}]: {item}")
                    point_list = item["indices"]
                    point_data = item["points"]

                    await write_point_sel(self._app, 
                                         chunk_id, 
                                         self._dset_json, 
                                         point_list, 
                                         point_data,
                                         bucket=self._bucket)
                    log.debug(f"read_point_sel - got 200 status for chunk_id: {chunk_id}")
                    status_code = 200
                else:
                    log.error(f"ChunkCrawler - unexpected action: {self._action}")
                    status_code = 500
                    break

            except ClientError as ce:
                status_code = 500
                log.error(f"ClientError {type(ce)} for read_chunk_hyperslab({chunk_id}): {ce} ")
            except CancelledError as cle:
                status_code = 503
                log.warn(f"CancelledError for read_chunk_hyperslab({chunk_id}): {cle}")
            except HTTPBadRequest as hbr:
                status_code = 400
                log.error(f"HTTPBadRequest for read_chunk_hyperslab({chunk_id}): {hbr} ")
            except HTTPNotFound as nfe:
                status_code = 404
                log.error(f"HTTPNotFoundRequest for read_chunk_hyperslab({chunk_id}): {nfe} ")
            except HTTPInternalServerError as ise:
                status_code = 500
                log.error(f"HTTPInternalServerError for read_chunk_hyperslab({chunk_id}): {ise} ")
            except Exception as e:
                status_code = 500
                log.error(f"Unexpected exception {type(e)} for read_chunk_hyperslab({chunk_id}): {e} ")
            retry += 1
            if status_code == 200 or retry == max_retries:
                break
            sleep_time = 1
            log.warn(f"ChunkCrawler.doWork - sleeping for {sleep_time}")
            time.sleep(sleep_time)

        # save status_code    
        self._status_map[chunk_id] = status_code
        if self._query is not None and status_code == 200:
            item = self._chunk_map[chunk_id]
            if 'query_rsp' in item:
                query_rsp = item['query_rsp']
                self._hits += len(query_rsp)
        log.info(f"ChunkCrawler - worker status for chunk {chunk_id}: {self._status_map[chunk_id]}")
           

async def PUT_Value(request):
    """
    Handler for PUT /<dset_uuid>/value request
    """
    log.request(request)
    app = request.app
    bucket = None
    body = None
    query = None
    json_data = None
    params = request.rel_url.query
    append_rows = None  # this is a append update or not
    append_dim = 0
    if "append" in params and params["append"]:
        try:
            append_rows = int(params["append"])
        except ValueError:
            msg = "invalid append query param"
            log.warn(msg)
            raise HTTPBadRequest(reason=msg)
        log.info(f"append_rows: {append_rows}")
        if "select" in params:
            msg = "select query parameter can not be used with packet updates"
            log.warn(msg)
            raise HTTPBadRequest(reason=msg)
    if "append_dim" in params and params["append_dim"]:
        try:
            append_dim = int(params["append_dim"])
        except ValueError:
            msg = "invalid append_dim"
            log.warn(msg)
            raise HTTPBadRequest(reason=msg)
        log.info(f"append_dim: {append_dim}")
   
    if "query" in params:
        if "append" in params:
            msg = "Query string can not be used with append parameter"
            log.warn(msg)
            raise HTTPBadRequest(reason=msg)
        query = params["query"]

    dset_id = request.match_info.get('id')
    if not dset_id:
        msg = "Missing dataset id"
        log.warn(msg)
        raise HTTPBadRequest(reason=msg)
    if not isValidUuid(dset_id, "Dataset"):
        msg = f"Invalid dataset id: {dset_id}"
        log.warn(msg)
        raise HTTPBadRequest(reason=msg)

    username, pswd = getUserPasswordFromRequest(request)
    await validateUserPassword(app, username, pswd)

    domain = getDomainFromRequest(request)
    if not isValidDomain(domain):
        msg = f"Invalid domain: {domain}"
        log.warn(msg)
        raise HTTPBadRequest(reason=msg)
    bucket = getBucketForDomain(domain)

    request_type = getContentType(request)
     
    log.debug(f"PUT value - request_type is {request_type}")
         
    if not request.has_body:
        msg = "PUT Value with no body"
        log.warn(msg)
        raise HTTPBadRequest(reason=msg)

    if request_type == "json":
        body = await request.json()
        if "append" in body and body["append"]:
            try:
                append_rows = int(body["append"])
            except ValueError:
                msg = "invalid append value in body"
                log.warn(msg)
                raise HTTPBadRequest(reason=msg)
            log.info(f"append_rows: {append_rows}")
        if append_rows:
            for key in ("start", "stop", "step"):
                if key in body:
                    msg = f"body key {key} can not be used with append"
                    log.warn(msg)
                    raise HTTPBadRequest(reason=msg)

        if "append_dim" in body and body["append_dim"]:
            try:
                append_dim = int(body["append_dim"])
            except ValueError:
                msg = "invalid append_dim"
                log.warn(msg)
                raise HTTPBadRequest(reason=msg)
            log.info(f"append_dim: {append_dim}")

    # get state for dataset from DN.
    dset_json = await getObjectJson(app, dset_id, bucket=bucket, refresh=False)

    layout = None
    datashape = dset_json["shape"]
    if datashape["class"] == 'H5S_NULL':
        msg = "Null space datasets can not be used as target for PUT value"
        log.warn(msg)
        raise HTTPBadRequest(reason=msg)

    dims = getShapeDims(datashape)
    maxdims = getDsetMaxDims(dset_json)
    rank = len(dims)

    if query and rank > 1:
        msg = "Query string is not supported for multidimensional arrays"
        log.warn(msg)
        raise HTTPBadRequest(reason=msg)

    layout = getChunkLayout(dset_json)

    type_json = dset_json["type"]
    dset_dtype = createDataType(type_json)
    item_size = getItemSize(type_json)
    
    if query:
        # divert here if we are doing a put query
        # returns array data like a GET query request
        if not checkQuery(query, dset_dtype):
            msg = f"query: {query} is not valid"
            log.warn(msg)
            raise HTTPBadRequest(reason=msg)
        
        select = params.get("select")
        slices = await get_slices(app, select, dset_json, bucket=bucket)
        if "Limit" in params:
            try:
                limit = int(params["Limit"])
            except ValueError:
                msg = "Limit param must be positive int"
                log.warning(msg)
                raise HTTPBadRequest(reason=msg)
        else:
            limit = 0

        arr_rsp = await getSelectionData(app, 
                                         dset_id, 
                                         dset_json, 
                                         slices,
                                         query=query,
                                         bucket=bucket,
                                         limit=limit,
                                         query_update=body,
                                         method=request.method)
     
        log.debug(f"arr shape: {arr_rsp.shape}")
        response_type = getAcceptType(request)
        if response_type == "binary":
            output_data = arr_rsp.tobytes()
            msg = f"PUT_Value query - returning {len(output_data)} bytes binary data"
            log.debug(msg)

            # write response
            try:
                resp = StreamResponse()
                if config.get("http_compression"):
                    log.debug("enabling http_compression")
                    resp.enable_compression()
                resp.headers['Content-Type'] = "application/octet-stream"
                resp.content_length = len(output_data)
                await resp.prepare(request)
                await resp.write(output_data)
                await resp.write_eof()
            except Exception as e:
                log.error(f"Exception during binary data write: {e}")
        else:
            log.debug("POST Value - returning JSON data")
            rsp_json = {}
            data = arr_rsp.tolist()
            log.debug(f"got rsp data {len(data)} points")
            json_data = bytesArrayToList(data)
            rsp_json["value"] = json_data
            rsp_json["hrefs"] = get_hrefs(request, dset_json)
            resp = await jsonResponse(request, rsp_json)
        log.response(request, resp=resp)
        return resp

    # Resume regular PUT_Value processing without query update
    dset_dtype = createDataType(type_json)  # np datatype
    binary_data = None
    np_shape = None  # expected shape of input data
    points = None  # used for point selection writes
    np_shape = []  # shape of incoming data
    slices = []    # selection area to write to

    if append_rows:
        # shape must be extensible
        if not isExtensible(dims, maxdims):
            msg = "Dataset shape must be extensible for packet updates"
            log.warn(msg)
            raise HTTPBadRequest(reason=msg)
        if append_dim < 0 or append_dim > rank-1:
            msg = "invalid append_dim"
            log.warn(msg)
            raise HTTPBadRequest(reason=msg)
        maxdims = getDsetMaxDims(dset_json)
        if maxdims[append_dim] != 0:
            if dims[append_dim] + append_rows > maxdims[append_dim]:
                log.warn("unable to append to dataspace")
                raise HTTPConflict()

    # refetch the dims if the dataset is extensible
    if isExtensible(dims, maxdims):
        kwargs = {"bucket": bucket, "refresh": True}
        dset_json = await getObjectJson(app, dset_id, **kwargs)
        dims = getShapeDims(dset_json["shape"])

    if request_type == "json":
        body_json = body
    else:
        body_json = None

    if request_type == "json":
        if "value" in body:
            json_data = body["value"]
        elif "value_base64" in body:
            base64_data = body["value_base64"]
            base64_data = base64_data.encode("ascii")
            binary_data = base64.b64decode(base64_data)
        else:
            msg = "PUT value has no value or value_base64 key in body"
            log.warn(msg)
            raise HTTPBadRequest(reason=msg)

        # body could also contain a point selection specifier
        if "points" in body:
            if append_rows:
                msg = "points not valid with packet update"
                log.warn(msg)
                raise HTTPBadRequest(reason=msg)

            json_points = body["points"]
            num_points = len(json_points)
            if rank == 1:
                point_shape = (num_points,)
                log.info(f"rank 1: point_shape: {point_shape}")
            else:
                point_shape = (num_points, rank)
                log.info(f"rank >1: point_shape: {point_shape}")
            try:
                # use uint64 so we can address large array extents
                dt = np.dtype(np.uint64)
                points = jsonToArray(point_shape, dt, json_points)
            except ValueError:
                msg = "Bad Request: point list not valid for dataset shape"
                log.warn(msg)
                raise HTTPBadRequest(reason=msg)
    else:
        # read binary data
        log.info(f"request content_length: {request.content_length}")
        max_request_size = int(config.get("max_request_size"))
        if isinstance(request.content_length, int):
            if request.content_length >= max_request_size:
                msg = f"Request size too large: {request.content_length} "
                msg += f"max: {max_request_size}"
                log.warn(msg)
                raise HTTPRequestEntityTooLarge(request.content_length,
                                                max_request_size)

        try:
            binary_data = await request_read(request)
        except HTTPRequestEntityTooLarge as tle:
            msg = "Got HTTPRequestEntityTooLarge exception during "
            msg += f"binary read: {tle})"
            log.warn(msg)
            raise  # re-throw

        if len(binary_data) != request.content_length:
            msg = f"Read {len(binary_data)} bytes, expecting: "
            msg += f"{request.content_length}"
            log.error(msg)
            raise HTTPBadRequest(reason=msg)

    if append_rows:
        for i in range(rank):
            if i == append_dim:
                np_shape.append(append_rows)
                # this will be adjusted once the dataspace is extended
                slices.append(slice(0, append_rows, 1))
            else:
                if dims[i] == 0:
                    dims[i] = 1  # need a non-zero extent for all dimensionas
                np_shape.append(dims[i])
                slices.append(slice(0, dims[i], 1))
        np_shape = tuple(np_shape)

    elif points is None:
        if body_json and "start" in body_json and "stop" in body_json:
            slices = await get_slices(app, body_json, dset_json, bucket=bucket)
        else:
            select = params.get("select")
            slices = await get_slices(app, select, dset_json, bucket=bucket)

        # The selection parameters will determine expected put value shape
        log.debug(f"PUT Value selection: {slices}")
        # not point selection, get hyperslab selection shape
        np_shape =  getSelectionShape(slices)
        num_elements = getNumElements(np_shape)
    else:
        # point update
        np_shape = (num_points,)
        num_elements = num_points
    log.debug(f"selection shape: {np_shape}")

    num_elements = getNumElements(np_shape)
    log.debug(f"selection num elements: {num_elements}")
    if num_elements <= 0:
        msg = "Selection is empty"
        log.warn(msg)
        raise HTTPBadRequest(reason=msg)

    arr = None  # np array to hold request data
    if binary_data and isinstance(item_size, int):
        # binary, fixed item_size
        if num_elements*item_size != len(binary_data):
            msg = f"Expected: {num_elements*item_size} bytes, "
            msg += f"but got: {len(binary_data)}"
            log.warn(msg)
            raise HTTPBadRequest(reason=msg)
        arr = np.fromstring(binary_data, dtype=dset_dtype)
        try:
            arr = arr.reshape(np_shape)  # conform to selection shape
        except ValueError:
            msg = "Bad Request: binary input data doesn't match selection"
            log.warn(msg)
            raise HTTPBadRequest(reason=msg)
        msg = f"PUT value - numpy array shape: {arr.shape} dtype: {arr.dtype}"
        log.debug(msg)
    elif binary_data and item_size == 'H5T_VARIABLE':
        # binary variable length data
        try:
            arr = bytesToArray(binary_data, dset_dtype, np_shape)
        except ValueError as ve:
            log.warn(f"bytesToArray value error: {ve}")
            raise HTTPBadRequest()
    else:
        #
        # data is json
        #
        try:
            msg = "input data doesn't match selection"
            arr = jsonToArray(np_shape, dset_dtype, json_data)
        except ValueError:
            log.warn(msg)
            raise HTTPBadRequest(reason=msg)
        except TypeError:
            log.warn(msg)
            raise HTTPBadRequest(reason=msg)
        except IndexError:
            log.warn(msg)
            raise HTTPBadRequest(reason=msg)
        log.debug(f"got json arr: {arr.shape}")

    if append_rows:
        # extend the shape of the dataset
        req = getDataNodeUrl(app, dset_id) + "/datasets/" + dset_id + "/shape"
        body = {"extend": append_rows, "extend_dim": append_dim}
        params = {}
        if bucket:
            params["bucket"] = bucket
        selection = None
        try:
            shape_rsp = await http_put(app, req, data=body, params=params)
            log.info(f"got shape put rsp: {shape_rsp}")
            if "selection" in shape_rsp:
                selection = shape_rsp["selection"]
        except HTTPConflict:
            log.warn("got 409 extending dataspace for PUT value")
            raise
        if not selection:
            log.error("expected to get selection in PUT shape response")
            raise HTTPInternalServerError()
        # selection should be in the format [:,n:m,:].
        # extract n and m and use it to update the slice for the
        # appending dimension
        if not selection.startswith("[") or not selection.endswith("]"):
            log.error("Unexpected selection in PUT shape response")
            raise HTTPInternalServerError()
        selection = selection[1:-1]  # strip off brackets
        parts = selection.split(',')
        for part in parts:
            if part == ":":
                continue
            bounds = part.split(':')
            if len(bounds) != 2:
                log.error("Unexpected selection in PUT shape response")
                raise HTTPInternalServerError()
            lb = ub = 0
            try:
                lb = int(bounds[0])
                ub = int(bounds[1])
            except ValueError:
                log.error("Unexpected selection in PUT shape response")
                raise HTTPInternalServerError()
            log.info(f"lb: {lb} ub: {ub}")
            # update the slices to indicate where to place the data
            slices[append_dim] = slice(lb, ub, 1)

    slices = tuple(slices)  # no more edits to slices
    crawler_status = None  # will be set below
    if points is None:
        # for hyperslab selection, verify the input shape matches the
        # selection
        np_index = 0
        for dim in range(len(arr.shape)):
            data_extent = arr.shape[dim]
            selection_extent = 1
            if np_index < len(np_shape):
                selection_extent = np_shape[np_index]
            if selection_extent == data_extent:
                np_index += 1
                continue  # good
            if data_extent == 1:
                continue  # skip singleton selection
            if selection_extent == 1:
                np_index += 1
                continue  # skip singleton selection

            # selection/data mismatch!
            msg = "data shape doesn't match selection shape"
            msg += "--data shape: " + str(arr.shape)
            msg += "--selection shape: " + str(np_shape)
            log.warn(msg)
            raise HTTPBadRequest(reason=msg)

        num_chunks = getNumChunks(slices, layout)
        log.debug(f"num_chunks: {num_chunks}")
        max_chunks = int(config.get('max_chunks_per_request'))
        if num_chunks > max_chunks:
            log.warn(f"PUT value chunk count: {num_chunks} exceeds max_chunks: {max_chunks}")

        try:
            chunk_ids = getChunkIds(dset_id, slices, layout)
        except ValueError:
            log.warn("getChunkIds failed")
            raise HTTPInternalServerError()
        log.debug(f"chunk_ids: {chunk_ids}")

        crawler = ChunkCrawler(app, 
                           chunk_ids, 
                           dset_json=dset_json,
                           bucket=bucket,
                           slices=slices,
                           arr=arr,
                           action="write_chunk_hyperslab")
        await crawler.crawl()

        crawler_status = crawler.get_status()

    else:
        #
        # Do point PUT
        #
        log.debug(f"num_points: {num_points}")

        chunk_dict = {}  # chunk ids to list of points in chunk

        for pt_indx in range(num_points):
            if rank == 1:
                point = int(points[pt_indx])
            else:
                point_tuple = points[pt_indx]
                point = []
                for i in range(len(point_tuple)):
                    point.append(int(point_tuple[i]))
            if rank == 1:
                if point < 0 or point >= dims[0]:
                    msg = f"PUT Value point: {point} is not within the "
                    msg += "bounds of the dataset"
                    log.warn(msg)
                    raise HTTPBadRequest(reason=msg)
            else:
                if len(point) != rank:
                    msg = "PUT Value point value did not match dataset rank"
                    log.warn(msg)
                    raise HTTPBadRequest(reason=msg)
                for i in range(rank):
                    if point[i] < 0 or point[i] >= dims[i]:
                        msg = f"PUT Value point: {point} is not within the "
                        msg += "bounds of the dataset"
                        log.warn(msg)
                        raise HTTPBadRequest(reason=msg)
            chunk_id = getChunkId(dset_id, point, layout)
            # get the pt_indx element from the input data
            value = arr[pt_indx]
            if chunk_id not in chunk_dict:
                point_list = [point, ]
                point_data = [value, ]
                chunk_dict[chunk_id] = {"indices": point_list,
                                        "points": point_data}
            else:
                item = chunk_dict[chunk_id]
                point_list = item["indices"]
                point_list.append(point)
                point_data = item["points"]
                point_data.append(value)

        num_chunks = len(chunk_dict)
        log.debug(f"num_chunks: {num_chunks}")
        max_chunks = int(config.get('max_chunks_per_request'))
        if num_chunks > max_chunks:
            msg = f"PUT value request with more than {max_chunks} chunks"
            log.warn(msg)

        chunk_ids = list(chunk_dict.keys())
        chunk_ids.sort()    

        crawler = ChunkCrawler(app, 
                           chunk_ids, 
                           dset_json=dset_json,
                           bucket=bucket,
                           points=chunk_dict,
                           action="write_point_sel")
        await crawler.crawl()

        crawler_status = crawler.get_status()

    if crawler_status == 400:
        log.info(f"doWriteSelection raising BadRequest error:  {crawler_status}")
        raise HTTPBadRequest()
    if crawler_status not in  (200, 201):
        log.info(f"doWriteSelection raising HTTPInternalServerError for status:  {crawler_status}")
        raise HTTPInternalServerError() 
    
    # write successful
    resp_json = {}
    resp = await jsonResponse(request, resp_json)
    return resp
    

async def GET_Value(request):
    """
    Handler for GET /<dset_uuid>/value request
    """
    log.request(request)
    app = request.app
    params = request.rel_url.query

    dset_id = request.match_info.get('id')
    if not dset_id:
        msg = "Missing dataset id"
        log.warn(msg)
        raise HTTPBadRequest(reason=msg)
    if not isValidUuid(dset_id, "Dataset"):
        msg = f"Invalid dataset id: {dset_id}"
        log.warn(msg)
        raise HTTPBadRequest(reason=msg)

    username, pswd = getUserPasswordFromRequest(request)
    if username is None and app['allow_noauth']:
        username = "default"
    else:
        await validateUserPassword(app, username, pswd)

    domain = getDomainFromRequest(request)
    if not isValidDomain(domain):
        msg = f"Invalid domain: {domain}"
        log.warn(msg)
        raise HTTPBadRequest(reason=msg)
    bucket = getBucketForDomain(domain)

    # get state for dataset from DN.
    dset_json = await getObjectJson(app, dset_id, bucket=bucket)   
    type_json = dset_json["type"]
    dset_dtype = createDataType(type_json)

    if isNullSpace(dset_json):
        msg = "Null space datasets can not be used as target for GET value"
        log.warn(msg)
        raise HTTPBadRequest(reason=msg)

    layout = getChunkLayout(dset_json)
    log.debug(f"chunk layout: {layout}")
    if "shm_name" in params and params["shm_name"]:
        shm_name = params["shm_name"]
    else:
        shm_name = None

    await validateAction(app, domain, dset_id, username, "read")

    # Get query parameter for selection 
    select = params.get("select")
    if select:
        log.debug(f"select query param: {select}")
    slices = await get_slices(app, select, dset_json, bucket=bucket)
    log.debug(f"GET Value selection: {slices}")

    limit = 0
    if "Limit" in params:
        try:
            limit = int(params["Limit"])
            log.debug(f"limit: {limit}")
        except ValueError:
            msg = "Invalid Limit query param"
            log.warn(msg)
            raise HTTPBadRequest(reason=msg)

    if "ignore_nan" in params and params["ignore_nan"]:
        ignore_nan = True
    else:
        ignore_nan = False
    log.debug(f"ignore nan: {ignore_nan}")

    content_length = None
    query = params.get("query")
    if query:
        if not checkQuery(query, dset_dtype):
            msg = f"query: {query} is not valid"
            log.warn(msg)
            raise HTTPBadRequest(reason=msg)
    else:
        # for non query requests with non-variable types we can fetch 
        # the expected response bytes length now
        item_size = getItemSize(type_json)
        log.debug(f"item size: {item_size}")

        # get the shape of the response array
        np_shape = getSelectionShape(slices)
        log.debug(f"selection shape: {np_shape}")

        # check that the array size is reasonable
        request_size = np.prod(np_shape)
        if item_size == 'H5T_VARIABLE':
            request_size *= 512  # random guess of avg item_size
        else:
            request_size *= item_size
        log.debug(f"request_size: {request_size}")
        max_request_size = int(config.get("max_request_size"))
        if request_size >= max_request_size:
            msg = "GET value request too large"
            log.warn(msg)
            raise HTTPRequestEntityTooLarge(request_size, max_request_size)
        if item_size != 'H5T_VARIABLE':
            # this is the exact number of bytes to be returned
            content_length = request_size

    if shm_name:
        response_type = "json"
    else:
        response_type = getAcceptType(request)

    resp_json = {"status": 200}  # will over-write if there's a problem
    arr = None
    # write response
    try:
        resp = StreamResponse()
        if config.get("http_compression"):
            log.debug("enabling http_compression")
            resp.enable_compression()
        if response_type == "binary":
            resp.headers['Content-Type'] = "application/octet-stream"
            if content_length is None:
                log.debug("content_length could not be determined")
            else:
                resp.content_length = content_length
        else:
            resp.headers['Content-Type'] = "application/json"
        log.debug("prepare request")
        await resp.prepare(request)

        try:
            arr = await getSelectionData(app, 
                           dset_id,
                           dset_json, 
                           slices, 
                           query=query,
                           bucket=bucket,
                           limit=limit,
                           method=request.method)
        except HTTPException as he:
            # close the response stream
            log.error(f"got {type(he)} exception doing getSelectionData: {he}")
            resp_json["status"] = he.status_code
            # can't raise a HTTPException here since write is in progress 

        if arr is None:
            # no array (OPTION request?)  Return empty json response
            log.warn("got None response from getSelectionData")

        elif not isinstance(arr, np.ndarray):
            msg = f"GET_Value - Expected ndarray but got: {type(arr)}"
            resp_json["status"] = 500
        elif shm_name:
            shm = None
            log.debug(f"attaching to shared memory block: {shm_name}")
            try:
                shm = shared_memory.SharedMemory(name=shm_name)
            except FileNotFoundError:
                msg = f"no shared memory block with name: {shm_name} found"
                log.warning(msg)
                resp_json["status"] = 400
            except OSError as oe:
                msg = f"Unexpected OSError: {oe.errno} attaching to shared memory block"
                log.error(msg)
                resp_json["status"] = 400
            if shm is not None:
                buffer = arrayToBytes(arr)
                num_bytes = len(buffer)
                if shm.size < num_bytes:
                    msg = f"unable to copy {num_bytes} to shared memory block of size: {shm.size}"
                    log.warning(msg)
                    resp_json["status"] = 413  # Payload too larger error

            # copy array data
            shm.buf[:num_bytes] = buffer[:]
            log.debug(f"copied {num_bytes} array data to shared memory name: {shm_name}")

            # close shared memory block
            # Note - since we are not calling shm.unlink (expecting the 
            # client to do that), it's likely the resource tracker will complain on
            # app exit.  This should be fixed in Python 3.9.  See: 
            # https://bugs.python.org/issue39959
            shm.close()

            log.debug("GET Value - returning JSON data with shared memory buffer")
            resp_json["shm_name"] = shm.name
            resp_json["num_bytes"] = num_bytes
            resp_json["hrefs"] = get_hrefs(request, dset_json)
            resp_body = await jsonResponse(resp, resp_json, body_only=True)
            resp_body = resp_body.encode('utf-8')
            await resp.write(resp_body)
        elif response_type == "binary":
            if resp_json["status"] != 200:
                # write json with status_code
                #resp_json = resp_json.encode('utf-8')
                #await resp.write(resp_json)
                log.warn(f"GET Value - got error status: {resp_json['status']}")
            else:
                log.debug("preparing binary response")
                output_data = arrayToBytes(arr)
                log.debug(f"got {len(output_data)} bytes for resp")
                log.debug("write request")
                await resp.write(output_data)
        else:
            # return json
            log.debug("GET Value - returning JSON data")
            params = request.rel_url.query
            if "reduce_dim" in params and params["reduce_dim"]:
                arr = squeezeArray(arr)
         
            data = arr.tolist()
            json_data = bytesArrayToList(data)
         
            datashape = dset_json["shape"]

            if datashape["class"] == 'H5S_SCALAR':
                # convert array response to value
                resp_json["value"] = json_data[0]
            else:
                resp_json["value"] = json_data
            resp_json["hrefs"] = get_hrefs(request, dset_json)
            resp_body = await jsonResponse(resp, resp_json, ignore_nan=ignore_nan, body_only=True)
            log.debug(f"jsonResponse returned: {resp_body}")
            resp_body = resp_body.encode('utf-8')
            await resp.write(resp_body)
        await resp.write_eof()
    except Exception as e:
        log.error(f"{type(e)} Exception during data write: {e}")
        import traceback
        tb = traceback.format_exc()
        print("traceback:", tb)
        raise HTTPInternalServerError()

    return resp
    

async def doReadSelection(app, chunk_ids, dset_json, 
                          slices=None,
                          points=None,
                          query=None,
                          query_update=None,
                          chunk_map=None, 
                          bucket=None, 
                          shm_name=None,
                          limit=0):
    """ read selection utility function """
    log.info(f"doReadSelection - number of chunk_ids: {len(chunk_ids)}")
    log.debug(f"doReadSelection - chunk_ids: {chunk_ids}")
     
    type_json = dset_json["type"]
    item_size = getItemSize(type_json)
    log.debug(f"item size: {item_size}")
    dset_dtype = createDataType(type_json)  # np datatype
    if query is None:
        query_dtype = None
    else:
        log.debug(f"query: {query} limit: {limit}")
        query_dtype = getQueryDtype(dset_dtype)

    # create array to hold response data
    arr = None
    
    if points is not None:
        # point selection
        np_shape = [len(points),]
    elif query is not None:
        # return shape will be determined by number of matches
        np_shape = None
    elif slices is not None:
        log.debug(f"get np_shape for slices: {slices}")
        np_shape = getSelectionShape(slices)
    else:
        log.error("doReadSelection - expected points or slices to be set")
        raise HTTPInternalServerError()
    log.debug(f"selection shape: {np_shape}")

    if np_shape is not None:
        # check that the array size is reasonable
        request_size = np.prod(np_shape)
        if item_size == 'H5T_VARIABLE':
            request_size *= 512  # random guess of avg item_size
        else:
            request_size *= item_size
            log.debug(f"request_size: {request_size}")
        max_request_size = int(config.get("max_request_size"))
        if request_size >= max_request_size:
            msg = f"Attempting to fetch {request_size} bytes (greater than {max_request_size} limit"
            log.error(msg)
            raise HTTPBadRequest(reason=msg)
  
        arr = np.zeros(np_shape, dtype=dset_dtype, order='C')
        fill_value = getFillValue(dset_json)
        if fill_value is not None:
            arr[...] = fill_value

    crawler = ChunkCrawler(app, 
                           chunk_ids, 
                           dset_json=dset_json,
                           chunk_map=chunk_map,
                           bucket=bucket,
                           slices=slices,
                           query=query,
                           query_update=query_update,
                           limit=limit,
                           arr=arr,
                           action="read_chunk_hyperslab")
    await crawler.crawl()

    crawler_status = crawler.get_status()

    log.info(f"doReadSelection complete - status:  {crawler_status}")
    if crawler_status == 400:
        log.info(f"doReadSelection raising BadRequest error:  {crawler_status}")
        raise HTTPBadRequest()
    if crawler_status not in  (200, 201):
        log.info(f"doReadSelection raising HTTPInternalServerError for status:  {crawler_status}")
        raise HTTPInternalServerError()

    if query is not None:
        # combine chunk responses and return
        if limit > 0 and crawler._hits > limit:
            nrows = limit
        else:
            nrows = crawler._hits
        arr = np.empty((nrows,),dtype=query_dtype)
        start = 0
        for chunkid in chunk_ids:
            if chunkid not in chunk_map:
                continue
            chunk_item = chunk_map[chunkid]
            if 'query_rsp' not in chunk_item:
                continue
            query_rsp = chunk_item['query_rsp']
            if len(query_rsp) == 0:
                continue
            stop = start + len(query_rsp)
            if stop > nrows:
                rsp_stop = len(query_rsp) - (stop - nrows)
                arr[start:] = query_rsp[0:rsp_stop]
            else:
                arr[start:stop] = query_rsp[:]
            start = stop
            if start >= nrows:
                log.debug(f"got {nrows} rows for query, quitting")
                break
    return arr


async def getSelectionData(app, dset_id, dset_json, slices=None, points=None, query=None, query_update=None, bucket=None, limit=0, method="GET"):
    """ Read selected slices and return numpy array """
    log.debug("getSelectionData")
    if slices is None and points is None:
        log.error("getSelectionData - expected either slices or points to be set")
        raise HTTPInternalServerError()

    layout = getChunkLayout(dset_json)

    chunkinfo = {}

    if slices is not None:
        num_chunks = getNumChunks(slices, layout)
        log.debug(f"num_chunks: {num_chunks}")
    
        max_chunks = int(config.get('max_chunks_per_request'))
        if num_chunks > max_chunks:
            msg = f"num_chunks over {max_chunks} limit, but will attempt to fetch with crawler"
            log.warn(msg)

        chunk_ids = getChunkIds(dset_id, slices, layout)
    else:
        # points - already checked it is not None
        num_points = len(points)
        chunk_ids = []
        for pt_indx in range(num_points):
            point = points[pt_indx]
            chunk_id = getChunkId(dset_id, point, layout)
            if chunk_id in chunkinfo:
                chunk_entry = chunkinfo[chunk_id]
            else:
                chunk_entry = {}
                chunkinfo[chunk_id] = chunk_entry
                chunk_ids.append(chunk_id)
            if 'points' in chunk_entry:
                point_list = chunk_entry['points']
            else:
                point_list = []
                chunk_entry['points'] = point_list
            if 'indices' in chunk_entry:
                point_index = chunk_entry['indices']
            else:
                point_index = []
                chunk_entry['indices'] = point_index
            
            point_list.append(point)
            point_index.append(pt_indx)
    


    # Get information about where chunks are located
    #   Will be None except for H5D_CHUNKED_REF_INDIRECT type
    await getChunkLocations(app,
                            dset_id,
                            dset_json,
                            chunkinfo,
                            chunk_ids,
                            bucket=bucket)
    if slices is None:
        slices = await get_slices(app, None, dset_json, bucket=bucket)

    if points is None:
        # get chunk selections for hyperslab select
        get_chunk_selections(chunkinfo, chunk_ids, slices, dset_json)

    log.debug(f"chunkinfo_map: {chunkinfo}")

    if method == "OPTIONS":
        # skip doing any big data load for options request
        return None

    arr = await doReadSelection(app,
                                chunk_ids,
                                dset_json,
                                slices=slices,
                                points=points,
                                query=query,
                                query_update=query_update,
                                limit=limit,
                                chunk_map=chunkinfo,
                                bucket=bucket)

    return arr

async def POST_Value(request):
    """
    Handler for POST /<dset_uuid>/value request - point selection or hyperslab read
    """
    log.request(request)

    app = request.app
    body = None

    dset_id = request.match_info.get('id')
    if not dset_id:
        msg = "Missing dataset id"
        log.warn(msg)
        raise HTTPBadRequest(reason=msg)
    if not isValidUuid(dset_id, "Dataset"):
        msg = f"Invalid dataset id: {dset_id}"
        log.warn(msg)
        raise HTTPBadRequest(reason=msg)

    log.info(f"POST_Value, dataset id: {dset_id}")

    username, pswd = getUserPasswordFromRequest(request)
    if username is None and app['allow_noauth']:
        username = "default"
    else:
        await validateUserPassword(app, username, pswd)

    domain = getDomainFromRequest(request)
    if not isValidDomain(domain):
        msg = f"Invalid domain: {domain}"
        log.warn(msg)
        raise HTTPBadRequest(reason=msg)
    bucket = getBucketForDomain(domain)

    accept_type = getAcceptType(request)
    response_type = accept_type  # will adjust later if binary not possible

    params = request.rel_url.query
    if "ignore_nan" in params and params["ignore_nan"]:
        ignore_nan = True
    else:
        ignore_nan = False

    request_type = getContentType(request)
    log.debug(f"POST value - request_type is {request_type}")
           
    if not request.has_body:
        msg = "POST Value with no body"
        log.warn(msg)
        raise HTTPBadRequest(reason=msg)

    # get  state for dataset from DN.
    dset_json = await getObjectJson(app, dset_id, bucket=bucket)

    datashape = dset_json["shape"]
    if datashape["class"] == 'H5S_NULL':
        msg = "POST value not supported for datasets with NULL shape"
        log.warn(msg)
        raise HTTPBadRequest(reason=msg)
    if datashape["class"] == 'H5S_SCALAR':
        msg = "POST value not supported for datasets with SCALAR shape"
        log.warn(msg)
        raise HTTPBadRequest(reason=msg)
    dims = getShapeDims(datashape)
    rank = len(dims)

    type_json = dset_json["type"]
    item_size = getItemSize(type_json)
    log.debug(f"item size: {item_size}")

    await validateAction(app, domain, dset_id, username, "read")

    # read body data
    slices = None  # this will be set for hyperslab selection
    points = None  # this will be set for point selection
    point_dt = np.dtype('u8')  # use unsigned long for point index
    if request_type == "json":
        body = await request.json()
        if "points" in body:
            points_list = body["points"]
            if not isinstance(points_list, list):
                msg = "POST Value expected list of points"
                log.warn(msg)
                raise HTTPBadRequest(reason=msg)
            points = np.asarray(points_list, dtype=point_dt)
            log.debug(f"get {len(points)} points from json request")
        elif "select" in body:
            select = body["select"]
            log.debug(f"select: {select}")
            slices = await get_slices(app, select, dset_json, bucket=bucket)
            log.debug(f"got slices: {slices}")
        else:
            msg = "Expected points or select key in request body"
            log.warn(msg)
            raise HTTPBadRequest(reason=msg)
    else:
        # read binary data
        binary_data = await request_read(request)
        if len(binary_data) != request.content_length:
            msg = f"Read {len(binary_data)} bytes, expecting: "
            msg += f"{request.content_length}"
            log.warn(msg)
            raise HTTPBadRequest(reason=msg)
        if request.content_length % point_dt.itemsize != 0:
            msg = f"Content length: {request.content_length} not "
            msg += f"divisible by element size: {item_size}"
            log.warn(msg)
            raise HTTPBadRequest(reason=msg)
        num_points = request.content_length // point_dt.itemsize
        points = np.fromstring(binary_data, dtype=point_dt)
        # reshape the data based on the rank (num_points x rank)
        if rank > 1:
            if len(points) % rank != 0:
                msg = "Number of point values is not consistent with dataset rank"
                log.warn(msg)
                raise HTTPBadRequest(reason=msg)
            num_points = len(points) // rank
            # conform to point index shape
            points = points.reshape((num_points, rank))

    if points is not None:
        log.debug(f"got {len(points)} num_points")

    # get expected content_length
    item_size = getItemSize(type_json)
    log.debug(f"item size: {item_size}")

    # get the shape of the response array
    if slices:
        # hyperslab post
        np_shape = getSelectionShape(slices)
    else:
        # point selection
        np_shape = [len(points),]

    log.debug(f"selection shape: {np_shape}")

    # check that the array size is reasonable
    request_size = np.prod(np_shape)
    if item_size == 'H5T_VARIABLE':
        request_size *= 512  # random guess of avg item_size
    else:
        request_size *= item_size
    log.debug(f"request_size: {request_size}")
    max_request_size = int(config.get("max_request_size"))
    if request_size >= max_request_size:
        msg = "POST value request too large"
        log.warn(msg)
        raise HTTPRequestEntityTooLarge(request_size, max_request_size)
    if item_size != 'H5T_VARIABLE':
        # this is the exact number of bytes to be returned
        content_length = request_size
    else:
        # don't put content_length in response headers
        content_length = None

    if points is not None:
        # validate content of points input array
        for i in range(len(points)):
            point = points[i]
            if rank == 1:
                if point < 0 or point >= dims[0]:
                    msg = f"POST Value point: {point} is not within the bounds "
                    msg += "of the dataset"
                    log.warn(msg)
                    raise HTTPBadRequest(reason=msg)
            else:
                if len(point) != rank:
                    msg = "POST Value point value did not match dataset rank"
                    log.warn(msg)
                    raise HTTPBadRequest(reason=msg)
                for i in range(rank):
                    if point[i] < 0 or point[i] >= dims[i]:
                        msg = f"POST Value point: {point} is not within the "
                        msg += "bounds of the dataset"
                        log.warn(msg)
                        raise HTTPBadRequest(reason=msg)

    # write response
    resp = StreamResponse()
    try: 
        if config.get("http_compression"):
            log.debug("enabling http_compression")
            resp.enable_compression()
        if response_type == "binary":
            resp.headers['Content-Type'] = "application/octet-stream"
            if content_length is None:
                log.debug("content_length could not be determined")
            else:
                resp.content_length = content_length
        else:
            resp.headers['Content-Type'] = "application/json"
        log.debug("prepare request...")
        await resp.prepare(request)

        kwargs = {'bucket': bucket}
        if slices is not None:
            kwargs['slices'] = slices
        if points is not None:
            kwargs['points'] = points
        log.debug(f"getSelectionData kwargs: {kwargs}")
        
        arr_rsp = await getSelectionData(app, dset_id, dset_json, **kwargs)
        if not isinstance(arr_rsp, np.ndarray):
            msg = f"POST_Value - Expected ndarray but got: {type(arr_rsp)}"
            log.error(msg)
            raise ValueError(msg)

        log.debug(f"arr shape: {arr_rsp.shape}")
        if response_type == "binary":
            log.debug("preparing binary response")
            output_data = arr_rsp.tobytes()
            msg = f"POST Value - returning {len(output_data)} bytes binary data"
            log.debug(msg)
            await resp.write(output_data)
        else:
            log.debug("POST Value - returning JSON data")
            resp_json = {}
            data = arr_rsp.tolist()
            log.debug(f"got rsp data {len(data)} points")
            json_data = bytesArrayToList(data)
            resp_json["value"] = json_data
            resp_json["hrefs"] = get_hrefs(request, dset_json)
            resp_body = await jsonResponse(resp, resp_json, ignore_nan=ignore_nan, body_only=True)
            log.debug(f"jsonResponse returned: {resp_body}")
            resp_body = resp_body.encode('utf-8')
            await resp.write(resp_body)
    except Exception as e:
        log.error(f"{type(e)} Exception during response write")
        import traceback
        tb = traceback.format_exc()
        print("traceback:", tb)
    # finalize response
    await resp.write_eof()
    
    log.response(request, resp=resp)
    return resp
