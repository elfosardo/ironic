# Copyright 2013 Red Hat, Inc.
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import jsonpatch
from oslo_config import cfg
from oslo_utils import uuidutils
import pecan
import six
from webob.static import FileIter
import wsme

from ironic.common import exception
from ironic.common.i18n import _
from ironic.common import utils
from ironic import objects


CONF = cfg.CONF


JSONPATCH_EXCEPTIONS = (jsonpatch.JsonPatchException,
                        jsonpatch.JsonPointerException,
                        KeyError)


def validate_limit(limit):
    if limit is None:
        return CONF.api.max_limit

    if limit <= 0:
        raise wsme.exc.ClientSideError(_("Limit must be positive"))

    return min(CONF.api.max_limit, limit)


def validate_sort_dir(sort_dir):
    if sort_dir not in ['asc', 'desc']:
        raise wsme.exc.ClientSideError(_("Invalid sort direction: %s. "
                                         "Acceptable values are "
                                         "'asc' or 'desc'") % sort_dir)
    return sort_dir


def apply_jsonpatch(doc, patch):
    for p in patch:
        if p['op'] == 'add' and p['path'].count('/') == 1:
            if p['path'].lstrip('/') not in doc:
                msg = _('Adding a new attribute (%s) to the root of '
                        ' the resource is not allowed')
                raise wsme.exc.ClientSideError(msg % p['path'])
    return jsonpatch.apply_patch(doc, jsonpatch.JsonPatch(patch))


def get_patch_value(patch, path):
    for p in patch:
        if p['path'] == path:
            return p['value']


def allow_node_logical_names():
    # v1.5 added logical name aliases
    return pecan.request.version.minor >= 5


def get_rpc_node(node_ident):
    """Get the RPC node from the node uuid or logical name.

    :param node_ident: the UUID or logical name of a node.

    :returns: The RPC Node.
    :raises: InvalidUuidOrName if the name or uuid provided is not valid.
    :raises: NodeNotFound if the node is not found.
    """
    # Check to see if the node_ident is a valid UUID.  If it is, treat it
    # as a UUID.
    if uuidutils.is_uuid_like(node_ident):
        return objects.Node.get_by_uuid(pecan.request.context, node_ident)

    # We can refer to nodes by their name, if the client supports it
    if allow_node_logical_names():
        if utils.is_hostname_safe(node_ident):
            return objects.Node.get_by_name(pecan.request.context, node_ident)
        raise exception.InvalidUuidOrName(name=node_ident)

    # Ensure we raise the same exception as we did for the Juno release
    raise exception.NodeNotFound(node=node_ident)


def is_valid_node_name(name):
    """Determine if the provided name is a valid node name.

    Check to see that the provided node name is valid, and isn't a UUID.

    :param: name: the node name to check.
    :returns: True if the name is valid, False otherwise.
    """
    return utils.is_hostname_safe(name) and (not uuidutils.is_uuid_like(name))


def vendor_passthru(ident, method, topic, data=None, driver_passthru=False):
    """Call a vendor passthru API extension.

    Call the vendor passthru API extension and process the method response
    to set the right return code for methods that are asynchronous or
    synchronous; Attach the return value to the response object if it's
    being served statically.

    :param ident: The resource identification. For node's vendor passthru
        this is the node's UUID, for driver's vendor passthru this is the
        driver's name.
    :param method: The vendor method name.
    :param topic: The RPC topic.
    :param data: The data passed to the vendor method. Defaults to None.
    :param driver_passthru: Boolean value. Whether this is a node or
        driver vendor passthru. Defaults to False.
    :returns: A WSME response object to be returned by the API.

    """
    if not method:
        raise wsme.exc.ClientSideError(_("Method not specified"))

    if data is None:
        data = {}

    http_method = pecan.request.method.upper()
    params = (pecan.request.context, ident, method, http_method, data, topic)
    if driver_passthru:
        response = pecan.request.rpcapi.driver_vendor_passthru(*params)
    else:
        response = pecan.request.rpcapi.vendor_passthru(*params)

    status_code = 202 if response['async'] else 200
    return_value = response['return']
    response_params = {'status_code': status_code}

    # Attach the return value to the response object
    if response.get('attach'):
        if isinstance(return_value, six.text_type):
            # If unicode, convert to bytes
            return_value = return_value.encode('utf-8')
        file_ = wsme.types.File(content=return_value)
        pecan.response.app_iter = FileIter(file_.file)
        # Since we've attached the return value to the response
        # object the response body should now be empty.
        return_value = None
        response_params['return_type'] = None

    return wsme.api.Response(return_value, **response_params)


def check_for_invalid_fields(fields, object_fields):
    """Check for requested non-existent fields.

    Check if the user requested non-existent fields.

    :param fields: A list of fields requested by the user
    :object_fields: A list of fields supported by the object.
    :raises: InvalidParameterValue if invalid fields were requested.

    """
    invalid_fields = set(fields) - set(object_fields)
    if invalid_fields:
        raise exception.InvalidParameterValue(
            _('Field(s) "%s" are not valid') % ', '.join(invalid_fields))


def check_allow_specify_fields(fields):
    """Check if fetching a subset of the resource attributes is allowed.

    Version 1.8 of the API allows fetching a subset of the resource
    attributes, this method checks if the required version is being
    requested.
    """
    if fields is not None and pecan.request.version.minor < 8:
        raise exception.NotAcceptable()
