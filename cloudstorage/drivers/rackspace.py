"""Rackspace Cloud Files Driver."""

import hashlib
import hmac
import logging

try:
    from http import HTTPStatus
except ImportError:
    # noinspection PyUnresolvedReferences
    from httpstatus import HTTPStatus
from time import time
from typing import Dict, Iterable, List, Tuple, Union
from urllib.parse import quote, urlencode, urljoin

# noinspection PyPackageRequirements
import dateutil.parser
import requests
from inflection import underscore
from openstack.exceptions import (HttpException, NotFoundException,
                                  ResourceNotFound)
from rackspace import connection
from rfc6266_parser import parse_headers

from cloudstorage.base import (Blob, Container, ContentLength, Driver,
                               ExtraOptions, FileLike, FormPost, MetaData)
from cloudstorage.exceptions import (CloudStorageError, IsNotEmptyError,
                                     NotFoundError)
from cloudstorage.helpers import file_content_type, validate_file_or_path
from cloudstorage.messages import *

logger = logging.getLogger(__name__)

MetaTempKey = Tuple[Union[str, None], Union[str, None]]


class CloudFilesDriver(Driver):
    """Driver for interacting with Rackspace Cloud Files.
    
    .. code-block:: python
        
        from cloudstorage.drivers.rackspace import CloudFilesDriver
        
        storage = CloudFilesDriver(key='<my-rackspace-username>',
                                   secret='<my-rackspace-secret-key>',
                                   region='IAD')
        # <Driver: CLOUDFILES IAD>

    References:

    * `Using OpenStack Object Store
      <https://developer.openstack.org/sdks/python/openstacksdk/users/guides/
      object_store.html>`_
    * `Object Store API
      <https://developer.openstack.org/sdks/python/openstacksdk/users/proxies/
      object_store.html>`_
    * `Working with Container Metadata <https://developer.openstack.org
      /sdks/python/openstacksdk/users/guides/object_store.html>`_
    * `CDN API reference - Rackspace Developer Portal 
      <https://developer.rackspace.com/docs/cloud-files/v1/
      cdn-api-reference/>`_

    .. todo:: Add support for RackspaceSDK ACL.

    :param key: Rackspace username.
    :type key: str

    :param secret: Rackspace secret key.
    :type secret: str

    :param region: (optional) Rackspace region. Defaults to `IAD`.
    
        * Dallas-Fort Worth (`DFW`)
        * Chicago (`ORD`)
        * Northern Virginia (`IAD`)
        * London (`LON`)
        * Sydney (`SYD`)
        * Hong Kong (`HKG`)
    :type region: str
    
    :param kwargs: (optional) Catch invalid options.
    :type kwargs: dict
    """
    name = 'CLOUDFILES'
    hash_type = 'md5'
    url = 'https://www.rackspace.com/cloud/files'

    def __init__(self, key: str, secret: str = None, region: str = 'IAD',
                 **kwargs: Dict) -> None:
        region = region.upper()
        if region not in self.regions:
            raise CloudStorageError(region_not_found % region)

        super().__init__(key=key, secret=secret, region=region)
        self._conn = connection.Connection(username=key, api_key=secret,
                                           region=region)

    def __iter__(self) -> Iterable[Container]:
        for cont in self.object_store.containers():
            yield self._make_container(cont)

    def __len__(self) -> int:
        containers = self.object_store.containers()
        return len(list(containers))

    @staticmethod
    def _normalize_parameters(params: Dict[str, str],
                              normalizers: Dict[str, str]) -> Dict[str, str]:
        """Transform parameter key names to match syntax required by the driver.

        :param params: Dictionary of parameters for method.
        :type params: dict

        :param normalizers: Dictionary mapping of key names.            
        :type normalizers: dict

        :return: Dictionary of transformed key names.

            ::

                {
                    '<key-name>': `<Mapped-Name>`
                    'meta_data': 'Metadata',
                    'content_disposition': 'ContentDisposition'
                }

        :rtype: Dict[str, str]
        """
        normalized = params.copy()

        for key, value in params.items():
            normalized.pop(key)

            key_inflected = underscore(key).lower()

            key_overrider = normalizers.get(key_inflected)
            if key_overrider:
                normalized[key_overrider] = value
            else:
                normalized[key_inflected] = value

        return normalized

    def _get_temp_url_key(self) -> str:
        """Get one of the account metadata keys for signing URLs.
        
        :return: Account metadata key.
        :rtype: str
        
        :raises CloudStorageError: If both account metadata keys are empty.
        """
        keys = self.get_account_temp_url_keys()
        try:
            return next(item for item in keys if item is not None)
        except StopIteration:
            raise CloudStorageError(
                "Please set a temporary URL key on the driver: "
                "'storage.set_account_temp_url_keys'")

    def _get_server_public_url(self, service_name: str) -> str:
        """Return the public endpoint URL for a particular service region.
        
        `https://storage101.iad3.clouddrive.com/v1/MossoCloudFS_XXXXX`

        :param service_name: Service name: `cloudFiles` or `cloudFilesCDN`.
        :type service_name: str

        :return: Public URL for the requested service.
        :rtype: str
        
        :raises CloudStorageError: If service name is not found in catalog.
        """
        service_catalog = self.conn.authenticator.auth_ref. \
            service_catalog.catalog

        for service in service_catalog:
            if service['name'] == service_name:
                for endpoint in service['endpoints']:
                    if endpoint['region'] == self.region:
                        return endpoint['publicURL']

        raise CloudStorageError(
            "Could not determine the public URL for '%s'." % service_name)

    def _get_container(self, container_name: str):
        """Get Rackspace container by name.
        
        :param container_name: Container name to get.
        :type container_name: str
        
        :return: Openstack object store container.
        :rtype: :class:`openstack.object_store.v1.container.Container`
        
        :raises NotFoundError: If container does not exist.
        """
        try:
            return self.object_store.get_container_metadata(container_name)
        except NotFoundException:
            raise NotFoundError(container_not_found % container_name)

    def _get_object(self, container_name: str, object_name: str):
        """Get Rackspace container by name.

        :param container_name: Container name that contains the object.
        :type container_name: str
        
        :param object_name: Object name to get.
        :type object_name: str

        :return: Openstack object store object.
        :rtype: :class:`openstack.object_store.v1.obj.Object`
        
        :raises NotFoundError: If object does not exist.
        """
        try:
            obj = self.object_store.get_object_metadata(
                obj=object_name, container=container_name)
        except (ResourceNotFound, NotFoundException):
            raise NotFoundError(blob_not_found % (object_name, container_name))

        return obj

    def _set_object_meta(self, obj, meta_data: MetaData) -> None:
        """Set object meta data.

        .. note:: The POST request to set metadata deletes all metadata that is 
                  not explicitly set in the request. In other words, ALL the 
                  object metadata is set at the time of the POST request. If 
                  you want to edit or remove one header, include all other 
                  headers in the POST request and leave out the header that 
                  you want to remove. This means that if you delete one entry 
                  without posting the others, the others will also be deleted 
                  at that time.

        References:

        * `Create or update object metadata 
          <https://developer.rackspace.com/docs/cloud-files/v1/
          storage-api-reference/object-services-operations/
          #create-or-update-object-metadata>`_

        .. todo:: Use conn.object_store.set_object_metadata when OpenStack SDK
                  fixes meta dictionary usage of `iteritems()` to `items()`. 

        :param obj: Openstack object instance.
        :type obj: :class:`openstack.object_store.v1.obj.Object`

        :param meta_data: A map of metadata to store with the object.
        :type meta_data: dict

        :return: NoneType
        :rtype: None
        
        :raises CloudStorageError: If setting the metadata failed. 
        """
        # TODO: BUG: Throws error due to legacy iteritems().
        # self.object_store.set_object_metadata(
        #     obj=obj, container=container.name, **meta_data)
        object_url = self._get_server_public_url('cloudFiles')
        object_url += '/' + quote(obj.container) + '/' + quote(obj.name)

        headers = {
            'X-Auth-Token': self._token
        }

        # Add header prefix to user meta data, X-Object-Meta-
        for meta_key, meta_value in meta_data.items():
            headers[self._OBJECT_META_PREFIX + meta_key] = meta_value

        # Include extra header params or they get deleted
        headers['X-Delete-At'] = obj.delete_at
        headers['X-Delete-After'] = obj.delete_after
        headers['Content-Type'] = obj.content_type
        headers['Content-Disposition'] = obj.content_disposition
        headers['Content-Encoding'] = obj.content_encoding

        response = requests.post(object_url, headers=headers)
        if response.status_code != HTTPStatus.ACCEPTED:
            raise CloudStorageError(response.text)

    def _set_container_meta(self, container: Container,
                            meta_data: MetaData) -> None:
        """Set metadata on container.

        :param container: Container to set metadata to.
        :type container: :class:`.Container`

        :param meta_data: A map of metadata to store with the container.
        :type meta_data: dict

        :return: NoneType
        :rtype: None
        
        :raises CloudStorageError: If setting the metadata failed. 
        """
        object_url = self._get_server_public_url('cloudFiles')
        object_url += '/' + quote(container.name)

        headers = {
            'X-Auth-Token': self._token
        }

        # Add header prefix to user meta data, X-Object-Meta-
        for meta_key, meta_value in meta_data.items():
            headers[self._CONTAINER_META_PREFIX + meta_key] = meta_value

        response = requests.post(object_url, headers=headers)
        if response.status_code != HTTPStatus.NO_CONTENT:
            raise CloudStorageError(response.text)

    def _make_container(self, cont) -> Container:
        """Convert Rackspace Container to Cloud Storage Container.

        :param cont: Openstack container to convert.
        :type cont: :class:`openstack.object_store.v1.container.Container`

        :return: A container instance.
        :rtype: :class:`.Container`
        """
        return Container(name=cont.name, driver=self, acl=None,
                         meta_data=cont.metadata, created_at=None)

    def _make_blob(self, container, obj) -> Blob:
        """Convert Rackspace Object to a Cloud Storage Blob.

        :param container: Container instance.
        :type container: :class:`.Container`

        :param obj: Openstack object instance.
        :type obj: :class:`openstack.object_store.v1.obj.Object`

        :return: Blob instance.
        :rtype: :class:`.Blob`
        """
        size = obj.bytes or int(obj.content_length)

        if obj.last_modified_at:
            modified_at = dateutil.parser.parse(obj.last_modified_at)
        elif obj.last_modified:
            modified_at = dateutil.parser.parse(obj.last_modified)
        else:
            modified_at = None

        if obj.delete_at:
            delete_at = obj.delete_at
        else:
            delete_at = None

        return Blob(name=obj.name, checksum=obj.hash or obj.etag, etag=obj.etag,
                    size=size, container=container, driver=self, acl=None,
                    meta_data=obj.metadata,
                    content_disposition=obj.content_disposition,
                    content_type=obj.content_type, created_at=None,
                    modified_at=modified_at, expires_at=delete_at)

    @property
    def _token(self) -> str:
        """Rackspace authentication token for manual requests.
        
        :return: Session token id.
        :rtype: str 
        """
        # noinspection PyProtectedMember
        return self.conn.authenticator.auth_ref._token['id']

    @property
    def conn(self):
        """Rackspace connection.
        
        :return: Rackspace connection.
        :rtype: rackspace.connection.Connection
        """
        return self._conn

    @property
    def object_store(self):
        """Rackspace object store proxy.
        
        :return: Proxy to Rackspace object store.
        :rtype: rackspace.object_store.v1._proxy.Proxy
        """
        # noinspection PyUnresolvedReferences
        return self.conn.object_store

    @property
    def regions(self) -> List[str]:
        return ['DFW', 'HKG', 'IAD', 'LON', 'ORD', 'SYD']

    def create_container(self, container_name: str, acl: str = None,
                         meta_data: MetaData = None) -> Container:
        if acl:
            logger.info(option_not_supported % 'acl')

        try:
            cont = self.object_store.create_container(
                **dict(name=container_name))
        except HttpException as e:
            raise CloudStorageError(e.details)

        meta_data = meta_data if meta_data is not None else {}
        self._set_container_meta(cont, meta_data)

        cont = self._get_container(cont.name)
        container = self._make_container(cont)

        # TODO: QUESTION: Automatically enable CDN for public-read?
        # if acl == 'public-read':
        #     self.enable_container_cdn(container)
        # else:
        #     logger.info(option_not_supported % 'acl')

        return container

    def get_container(self, container_name: str) -> Container:
        container = self._get_container(container_name)
        return self._make_container(container)

    def patch_container(self, container: Container) -> None:
        cont = self._get_container(container.name)

        cont.metadata.update(container.meta_data)
        self.object_store.set_container_metadata(cont, **container.meta_data)

        diff = set(cont.metadata.keys()) - set(container.meta_data.keys())
        self.object_store.delete_container_metadata(container=cont, keys=diff)

    def delete_container(self, container: Container) -> None:
        try:
            self.object_store.delete_container(container.name)
        except ResourceNotFound:
            raise NotFoundError(container_not_found % container.name)
        except HttpException as e:
            if e.http_status == HTTPStatus.CONFLICT:
                raise IsNotEmptyError(container_not_empty % container.name)
            raise CloudStorageError(e.details)

    def container_cdn_url(self, container: Container) -> str:
        endpoint_url = self._get_server_public_url(
            'cloudFilesCDN') + '/' + container.name
        headers = {
            'X-Auth-Token': self._token,
        }
        response = requests.head(endpoint_url, headers=headers)

        uri = response.headers.get('x-cdn-ssl-uri')
        if not uri:
            raise CloudStorageError(cdn_not_enabled % container.name)

        return uri

    def enable_container_cdn(self, container: Container) -> bool:
        endpoint_url = self._get_server_public_url(
            'cloudFilesCDN') + '/' + container.name
        headers = {
            'X-Auth-Token': self._token,
            'X-CDN-Enabled': str(True),
        }

        response = requests.put(endpoint_url, headers=headers)
        return response.status_code in (
            HTTPStatus.CREATED,
            HTTPStatus.ACCEPTED,
            HTTPStatus.NO_CONTENT)

    def disable_container_cdn(self, container: Container) -> bool:
        endpoint_url = self._get_server_public_url(
            'cloudFilesCDN') + '/' + container.name
        headers = {
            'X-Auth-Token': self._token,
            'X-CDN-Enabled': str(False),
        }

        response = requests.put(endpoint_url, headers=headers)
        return response.status_code in (
            HTTPStatus.CREATED, HTTPStatus.ACCEPTED, HTTPStatus.NO_CONTENT)

    def upload_blob(self, container: Container, filename: Union[str, FileLike],
                    blob_name: str = None, acl: str = None,
                    meta_data: MetaData = None, content_type: str = None,
                    content_disposition: str = None, chunk_size: int = 1024,
                    extra: ExtraOptions = None) -> Blob:
        if acl:
            logger.warning(option_not_supported % 'acl')

        meta_data = meta_data if meta_data is not None else {}
        extra = extra if extra is not None else {}

        extra_norm = self._normalize_parameters(extra, self._OBJECT_META_KEYS)
        extra_norm.setdefault('content_encoding', None)
        extra_norm.setdefault('delete_at', None)
        extra_norm.setdefault('delete_after', None)

        blob_name = blob_name or validate_file_or_path(filename)

        if not content_type:
            if isinstance(filename, str):
                content_type = file_content_type(filename)
            else:
                content_type = file_content_type(blob_name)

        if isinstance(filename, str):
            file_obj = open(filename, 'rb')
        else:
            file_obj = filename

        with file_obj as data:
            # returns an obj but bytes and content-length are empty
            self.object_store.upload_object(**dict(
                container=container.name, name=blob_name, data=data,
                content_type=content_type,
                content_disposition=content_disposition,
                content_encoding=extra_norm['content_encoding'],
                delete_after=extra_norm['delete_after'],
                delete_at=extra_norm['delete_at']))

        # Manually set meta data after object upload
        obj = self._get_object(container.name, blob_name)
        self._set_object_meta(obj, meta_data)

        obj = self._get_object(container.name, blob_name)
        return self._make_blob(container, obj)

    def get_blob(self, container: Container, blob_name: str) -> Blob:
        obj = self._get_object(container.name, blob_name)
        return self._make_blob(container, obj)

    def get_blobs(self, container: Container) -> Iterable[Blob]:
        for obj in self.object_store.objects(container.name):
            yield self._make_blob(container, obj)

    def download_blob(self, blob: Blob,
                      destination: Union[str, FileLike]) -> None:
        try:
            data = self.object_store.get_object(
                obj=blob.name, container=blob.container.name)

            if isinstance(destination, str):
                with open(destination, 'wb') as out:
                    out.write(data)
            else:
                destination.write(data)
        except ResourceNotFound:
            raise NotFoundError(blob_not_found % (blob.name,
                                                  blob.container.name))

    def patch_blob(self, blob: Blob) -> None:
        obj = self._get_object(blob.container.name, blob.name)

        obj.metadata.update(blob.meta_data)
        self.object_store.set_object_metadata(
            obj=obj, container=blob.container.name, **blob.meta_data)

        diff = set(obj.metadata.keys()) - set(blob.meta_data.keys())
        self.object_store.delete_object_metadata(
            obj=obj, container=blob.container.name, keys=diff)

    def delete_blob(self, blob: Blob) -> None:
        try:
            self.object_store.delete_object(obj=blob.name, ignore_missing=False,
                                            container=blob.container.name)
        except ResourceNotFound:
            raise NotFoundError(blob_not_found % (blob.name,
                                                  blob.container.name))

    def blob_cdn_url(self, blob: Blob) -> str:
        container_cdn_url = self.container_cdn_url(blob.container)
        url = urljoin(container_cdn_url, quote(blob.name))
        return url

    def generate_container_upload_url(self, container: Container,
                                      blob_name: str,
                                      expires: int = 3600, acl: str = None,
                                      meta_data: MetaData = None,
                                      content_disposition: str = None,
                                      content_length: ContentLength = None,
                                      content_type: str = None,
                                      extra: ExtraOptions = None) -> FormPost:
        if acl:
            logger.warning(option_not_supported % 'acl')

        if meta_data:
            logger.warning(option_not_supported % 'meta_data')

        if content_disposition:
            logger.warning(option_not_supported % 'content_disposition')

        if content_type:
            logger.warning(option_not_supported % 'content_type')

        extra = extra if extra is not None else {}
        extra_norm = self._normalize_parameters(extra, self._POST_OBJECT_KEYS)

        key = self._get_temp_url_key()
        storage_public_url = self._get_server_public_url('cloudFiles')

        # POST URL and path field
        url = '%s/%s/%s' % (storage_public_url, quote(container.name),
                            quote(blob_name))
        base_url, container_path = url.split('/v1/')
        path = '/v1/' + container_path

        fields = {}

        # Optional parameters and attributes
        redirect = (extra_norm.get('success_action_redirect') or
                    extra_norm.get('redirect'))
        fields['redirect'] = '' if redirect is None else redirect

        # Required parameters and attributes
        fields['path'] = path
        fields['max_file_count'] = 1

        if content_length:
            fields['max_file_size'] = content_length[1]
        else:
            fields['max_file_size'] = 5000000000  # 5 GB default

        # Time must be in UNIX epoch format.
        fields['expires'] = int(time() + expires)

        hmac_body = '{path}\n' \
                    '{redirect}\n' \
                    '{max_file_size}\n' \
                    '{max_file_count}\n' \
                    '{expires}'.format(**fields)
        signature = hmac.new(key.encode('utf-8'), hmac_body.encode('utf-8'),
                             hashlib.sha1).hexdigest()
        fields['signature'] = signature

        return {'url': url, 'fields': fields}

    def generate_blob_download_url(self, blob: Blob, expires: int = 3600,
                                   method: str = 'GET',
                                   content_disposition: str = None,
                                   extra: ExtraOptions = None) -> str:
        if extra:
            logger.info(option_not_supported % 'extra')

        key = self._get_temp_url_key()
        storage_public_url = self._get_server_public_url('cloudFiles')

        url = '%s/%s' % (storage_public_url, quote(blob.path))
        base_url, object_path = url.split('/v1/')
        object_path = '/v1/' + object_path

        # Time must be in UNIX epoch format.
        expires_in = int(time() + expires)

        hmac_body = '%s\n%s\n%s' % (method.upper(), expires_in, object_path)
        signature = hmac.new(key.encode('utf-8'), hmac_body.encode('utf-8'),
                             hashlib.sha1).hexdigest()

        parameters = {
            'temp_url_sig': signature,
            'temp_url_expires': expires_in,
        }

        # Rackspace uses query params: filename (attachment) and inline
        if content_disposition:
            disposition_parsed = parse_headers(content_disposition)

            if disposition_parsed.filename_unsafe:
                parameters['filename'] = disposition_parsed.filename_unsafe
            else:
                parameters['filename'] = blob.name

            if disposition_parsed.disposition == 'inline':
                parameters['inline'] = ''

        return '%s%s?%s' % (base_url, object_path, urlencode(parameters))

    def get_account_temp_url_keys(self) -> MetaTempKey:
        """Return URL meta keys for signing temporary URLs.

        For example:
        
        .. code-block:: python
            
            storage.get_account_temp_url_keys()
            # ('<meta_temp_url_key>', '<meta_temp_url_key_2>')

        References:

        * `Public access to your Cloud Files account 
          <https://developer.rackspace.com/docs/cloud-files/v1/use-cases/
          public-access-to-your-cloud-files-account/>`_

        :return: Tuple of both temporary URL keys.
        :rtype: tuple
        """
        account = self.object_store.get_account_metadata()
        return account.meta_temp_url_key, account.meta_temp_url_key_2

    def set_account_temp_url_keys(self, temp_url_key: str = None,
                                  temp_url_key_2: str = None) -> MetaTempKey:
        """Set URL meta keys for signing temporary URLs.

        For example:
        
        .. code-block:: python
            
            # Set key
            storage.set_account_temp_url_keys(temp_url_key_2='<my-new-key>')
            # ('<my-key>', '<my-new-key>')

            # Delete key
            storage.set_account_temp_url_keys(temp_url_key_2='')
            # ('<my-key>', None)

        References:

        * `Public access to your Cloud Files account <https://developer.
          rackspace.com/docs/cloud-files/v1/use-cases/public-access-to-your-
          cloud-files-account/>`_

        :param temp_url_key: (optional) First signing key.
        :type temp_url_key: str or None

        :param temp_url_key_2: (optional) Second signing key.
        :type temp_url_key_2: str or None

        :return: Tuple of both temporary URL keys.
        :rtype: tuple
        """
        meta_data = {
            'temp_url_key': temp_url_key,
            'temp_url_key_2': temp_url_key_2,
        }
        self.object_store.set_account_metadata(**meta_data)
        return self.get_account_temp_url_keys()

    _OBJECT_META_PREFIX = 'X-Object-Meta-'
    _CONTAINER_META_PREFIX = 'X-Container-Meta-'
    _CONTAINER_DELETE_META_PREFIX = 'X-Remove-Container'

    # TODO: CODE: Differentiate between keys for POST, GET, PUT, and DELETE.
    #: `formpost
    #: <https://developer.rackspace.com/docs/cloud-files/v1/use-cases/
    #: public-access-to-your-cloud-files-account/#formpost>`_
    _POST_OBJECT_KEYS = {
        'max_file_size': 'max_file_size',
        'max_file_count': 'max_file_count',
        'redirect': 'redirect',
    }

    #: `object_store
    #: <https://developer.openstack.org/sdks/python/openstacksdk/users/proxies/
    #: object_store.html>`_
    _OBJECT_META_KEYS = {
        'content_type': 'content_type',
        'content_encoding': 'content_encoding',
        'content_disposition': 'content_disposition',
        'delete_after': 'delete_after',
        'delete_ae': 'delete_at',
        'is_content_type_detected': 'is_content_type_detected',
    }

    #: `object_store
    #: <https://developer.openstack.org/sdks/python/openstacksdk/users/proxies/
    #: object_store.html>`_
    _CONTAINER_META_KEYS = {
        'content_type': 'content_type',
        'is_content_type_detected': 'is_content_type_detected',
        'versions_location': 'versions_location',
        'read_ACL': 'read_ACL',
        'write_ACL': 'write_ACL',
        'sync_to': 'sync_to',
        'sync_key': 'sync_key',
    }

    #: `create-or-update-container-metadata
    #: <https://developer.rackspace.com/docs/cloud-files/v1/
    # storage-api-reference/container-services-operations/
    # #create-or-update-container-metadata>`_
    _CONTAINER_POST_KEYS = {
        'read': 'X-Container-Read',
        'write': 'X-Container-Write',
        'version': 'X-Versions-Location',
        'content_type': 'Content-Type',
        'detect-content-type': 'X-Detect-Content-Type',
    }
