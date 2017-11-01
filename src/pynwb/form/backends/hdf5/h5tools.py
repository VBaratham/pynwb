from collections import Iterable
import numpy as np
import os.path
from h5py import File, Group, Dataset, special_dtype, SoftLink, ExternalLink, Reference, RegionReference
from six import raise_from, text_type, string_types, binary_type
from functools import partial

from ...container import Container

from ...utils import docval, getargs, popargs, call_docval_func
from ...data_utils import DataChunkIterator, get_shape
from ...build import Builder, GroupBuilder, DatasetBuilder, LinkBuilder, BuildManager, RegionBuilder, TypeMap
from ...spec import RefSpec, DtypeSpec, NamespaceCatalog, SpecWriter, SpecReader

from ..io import FORMIO

ROOT_NAME = 'root'
SPEC_LOC_ATTR = '.specloc'

class HDF5IO(FORMIO, ):

    @docval({'name': 'path', 'type': str, 'doc': 'the path to the HDF5 file to write to'},
            {'name': 'manager', 'type': BuildManager, 'doc': 'the BuildManager to use for I/O', 'default': None},
            {'name': 'mode', 'type': str, 'doc': 'the mode to open the HDF5 file with, one of ("w", "r", "r+", "a", "w-")', 'default': 'a'})
    def __init__(self, **kwargs):
        '''Open an HDF5 file for IO

        For `mode`, see :ref:`write_nwbfile`
        '''
        path, manager, mode = popargs('path', 'manager', 'mode', kwargs)
        if manager is None:
            manager = BuildManager(TypeMap(NamespaceCatalog()))
        super(HDF5IO, self).__init__(manager, source=path)
        self.__path = path
        self.__mode = mode
        self.__built = dict()
        self.__file = None
        self.__read = dict()
        self.__ref_queue = list()

    @classmethod
    @docval({'name': 'group', 'type': Group, 'doc': 'the group to write the namespace to'},
            {'name': 'namespace', 'type': SpecNamespace, 'doc': 'the namespace to write'},
            {'name': 'data_types', 'type': Iterable, 'doc': 'the data types to write', 'default': None},
            {'name': 'ns_dset_name', 'type': str, 'doc': 'the name of the namespace dataset', 'default': 'namespace'})
    def cache_spec(cls, group, namespace, dataset_name='namespace'):
        '''
        Write a specification namespace to the underlying HDF5 file.

        Each type will be written as JSON formatted string in a scalar dataset named
        using the type's data_type_def.
        '''
        group, namespace, data_types, ns_dset_name = getargs('group', 'namespace', 'data_types', 'ns_dset_name', kwargs)
        if data_types is None:
            data_types = namespace.get_registered_types()
        writer = H5SpecWriter(group)
        writer.write_namespace(namespace, ns_dset_name)
        for spec_name in data_types:
            spec = namespace.get_spec(spec_name)
            writer.write_spec(spec, spec.data_type_def)

    @docval({'name': 'container', 'type': Container, 'doc': 'the Container object to write'},
            {'name': 'cache_spec', 'type': bool, 'doc': 'cache specification to file', 'default': False}) # TODO: make this True by default
    def write(self, **kwargs):
        call_docval_func(super(HDF5IO, self).write, kwargs)
        cache_spec = getargs('cach_spec', kwargs)
        if cache_spec:
            ref = self.__file.attrs.get(SPEC_LOC_ATTR)
            spec_group = None
            if ref is not None:
                spec_group = self.__file[ref]
            else:
                path = 'specifications' # do something to figure out where the specifications should go
                spec_group = self.__file.require_group(path)
                self.__file.attrs[SPEC_LOC_ATTR] = spec_group.ref
            ns_catalog = self.__manager.ns_catalog
            for ns_name in ns_catalog.namespaces:
                namespace = ns_catalog.get_namespace(ns_name)
                group_name = '%s/%s' % (ns.name, namespace.version)
                ns_group = spec_group.require_group(group_name)
                it = chain(*(nsc.get_types(f) for f in nsc.get_namespace_sources(ns_name)))
                self.cache_spec(ns_group, namespace, data_types=it)

    @docval(returns='a GroupBuilder representing the NWB Dataset', rtype='GroupBuilder')
    def read_builder(self):
        self.open()
        f_builder = self.__read.get(self.__file)
        if f_builder is None:
            f_builder = self.__read_group(self.__file, ROOT_NAME)
            self.__read[self.__file] = f_builder
        return f_builder

    def __set_built(self, fpath, path, builder):
        self.__built.setdefault(fpath, dict()).setdefault(path, builder)

    def __get_built(self, fpath, path):
        fdict = self.__built.get(fpath)
        if fdict:
            return fdict.get(path)
        else:
            return None

    def __read_group(self, h5obj, name=None):
        kwargs = {
            "attributes": dict(h5obj.attrs.items()),
            "groups": dict(),
            "datasets": dict(),
            "links": dict()
        }
        if name is None:
            name = os.path.basename(h5obj.name)
        for k in h5obj:
            sub_h5obj = h5obj.get(k)
            link_type = h5obj.get(k, getlink=True)
            if isinstance(link_type, SoftLink) or isinstance(link_type, ExternalLink):
                # get path of link (the key used for tracking what's been built)
                target_path = link_type.path
                builder_name = os.path.basename(target_path)
                # get builder if already read, else build it
                builder = self.__get_built(sub_h5obj.file.filename, target_path)
                if builder is None:
                    # NOTE: all links must have absolute paths
                    if isinstance(sub_h5obj, Dataset):
                        builder = self.__read_dataset(sub_h5obj, builder_name)
                    else:
                        builder = self.__read_group(sub_h5obj, builder_name)
                    self.__set_built(sub_h5obj.file.filename, target_path, builder)
                kwargs['links'][builder_name] = LinkBuilder(k, builder, source=self.__path)
            else:
                builder = self.__get_built(sub_h5obj.file.filename, sub_h5obj.name)
                obj_type = None
                read_method = None
                if isinstance(sub_h5obj, Dataset):
                    read_method = self.__read_dataset
                    obj_type = kwargs['datasets']
                else:
                    read_method = self.__read_group
                    obj_type = kwargs['groups']
                if builder is None:
                    builder = read_method(sub_h5obj)
                    self.__set_built(sub_h5obj.file.filename, sub_h5obj.name, builder)
                obj_type[builder.name] = builder
        kwargs['source'] = self.__path
        ret = GroupBuilder(name, **kwargs)
        return ret

    def __read_dataset(self, h5obj, name=None):
        kwargs = {
            "attributes": dict(h5obj.attrs.items()),
            "dtype": h5obj.dtype,
            "maxshape": h5obj.maxshape
        }
        if name is None:
            name = os.path.basename(h5obj.name)
        kwargs['source'] = self.__path
        ndims = len(h5obj.shape)
        cls = DatasetBuilder
        if ndims == 0:                                       # read scalar
            scalar = h5obj[()]
            if isinstance(scalar, RegionReference):
                cls = RegionBuilder
                target = h5obj.file[scalar]
                target_builder = self.__read_dataset(target)
                self.__set_built(target.file.filename, target.name, target_builder)
                kwargs['builder'] = target_builder
                kwargs['region'] = scalar
                kwargs.pop('dtype')
                kwargs.pop('maxshape')
            else:
                kwargs["data"] = scalar
        elif ndims == 1 and h5obj.dtype == np.dtype('O'):    # read list of strings
            kwargs["data"] = list(h5obj[()])
        else:
            kwargs["data"] = h5obj
        ret = cls(name, **kwargs)
        return ret

    def open(self):
        open_flag = self.__mode
        self.__file = File(self.__path, open_flag)

    def close(self):
        self.__file.close()

    @docval({'name': 'builder', 'type': GroupBuilder, 'doc': 'the GroupBuilder object representing the NWBFile'})
    def write_builder(self, **kwargs):
        f_builder = getargs('builder', kwargs)
        self.open()
        for name, gbldr in f_builder.groups.items():
            self.write_group(self.__file, gbldr)
        for name, dbldr in f_builder.datasets.items():
            self.write_dataset(self.__file, dbldr)
        self.set_attributes(self.__file, f_builder.attributes)
        self.__add_refs()

    def __add_refs(self):
        '''
        Add all references in the file.

        References get queued to be added at the end of write. This is because
        the current traversal algorithm (i.e. iterating over GroupBuilder items)
        does not happen in a guaranteed order. We need to figure out what objects
        will be references, and then write them after we write everything else.
        '''
        while len(self.__ref_queue) > 0:
            call = self.__ref_queue.pop()
            call()

    @classmethod
    def get_type(cls, data):
        if isinstance(data, (text_type, string_types)):
            return special_dtype(vlen=text_type)
        elif not hasattr(data, '__len__'):
            return type(data)
        else:
            if len(data) == 0:
                raise ValueError('cannot determine type for empty data')
            return cls.get_type(data[0])

    __dtypes = {
        "float": np.float32,
        "float32": np.float32,
        "double" : np.float64,
        "float64": np.float64,
        "long" : np.int64,
        "int64" : np.int64,
        "int": np.int32,
        "int32": np.int32,
        "int16": np.int16,
        "int8": np.int8,
        "text": special_dtype(vlen=text_type),
        "utf": special_dtype(vlen=text_type),
        "utf8": special_dtype(vlen=text_type),
        "utf-8": special_dtype(vlen=text_type),
        "ascii": special_dtype(vlen=binary_type),
        "str": special_dtype(vlen=binary_type),
        "uint32": np.uint32,
        "uint16": np.uint16,
        "int16": np.int16,
        "uint8": np.uint8,
        "ref": special_dtype(ref=Reference),
        "reference": special_dtype(ref=Reference),
        "object": special_dtype(ref=Reference),
        "region": special_dtype(ref=RegionReference)
    }

    @classmethod
    def __resolve_dtype__(cls, dtype, data):
        # TODO: These values exist, but I haven't solved them yet
        # binary
        # number
        dtype = cls.__resolve_dtype_helper__(dtype)
        if dtype is None:
            try:
                dtype = cls.get_type(data)
            except Exception as exc:
                msg = 'cannot add %s to %s - could not determine type' % (name, parent.name)
                raise_from(Exception(msg), exc)
        return dtype

    @classmethod
    def __resolve_dtype_helper__(cls, dtype):
        if dtype is None:
            return None
        elif isinstance(dtype, str):
            return cls.__dtypes.get(dtype)
        elif isinstance(dtype, dict):
            return cls.__dtypes.get(dtype['reftype'])
        else:
            return np.dtype([(x['name'], cls.__resolve_dtype_helper__(x['dtype'])) for x in dtype])

    @classmethod
    @docval({'name': 'obj', 'type': (Group, Dataset), 'doc': 'the HDF5 object to add attributes to'},
            {'name': 'attributes', 'type': dict, 'doc': 'a dict containing the attributes on the Group, indexed by attribute name'})
    def set_attributes(cls, **kwargs):
        obj, attributes = getargs('obj', 'attributes', kwargs)
        for key, value in attributes.items():
            if any(isinstance(value, t) for t in (set, list, tuple)):
                tmp = tuple(value)
                if len(tmp) > 0:
                    if isinstance(tmp[0], str):
                        max_len = max(len(s) for s in tmp)
                        dt = '|S%d' % max_len
                        value = np.array(tmp, dtype=dt)
                    value = np.array(value)
            obj.attrs[key] = value

    @docval({'name': 'parent', 'type': Group, 'doc': 'the parent HDF5 object'},
            {'name': 'builder', 'type': GroupBuilder, 'doc': 'the GroupBuilder to write'},
            returns='the Group that was created', rtype='Group')
    def write_group(self, **kwargs):

        parent, builder = getargs('parent', 'builder', kwargs)
        group = parent.create_group(builder.name)
        # write all groups
        subgroups = builder.groups
        if subgroups:
            for subgroup_name, sub_builder in subgroups.items():
                # do not create an empty group without attributes or links
                self.write_group(group, sub_builder)
        # write all datasets
        datasets = builder.datasets
        if datasets:
            for dset_name, sub_builder in datasets.items():
                self.write_dataset(group, sub_builder)
        # write all links
        links = builder.links
        if links:
            for link_name, sub_builder in links.items():
                self.write_link(group, sub_builder)
        attributes = builder.attributes
        self.set_attributes(group, attributes)
        return group

    def __get_path(self, builder):
        curr = builder
        names = list()
        while curr is not None and curr.name != ROOT_NAME:
            names.append(curr.name)
            curr = curr.parent
        delim = "/"
        path = "%s%s" % (delim, delim.join(reversed(names)))
        return path

    @docval({'name': 'parent', 'type': Group, 'doc': 'the parent HDF5 object'},
            {'name': 'builder', 'type': LinkBuilder, 'doc': 'the LinkBuilder to write'},
            returns='the Link that was created', rtype='Link')
    def write_link(self, **kwargs):
        parent, builder = getargs('parent', 'builder', kwargs)
        name = builder.name
        target_builder = builder.builder
        path = self.__get_path(target_builder)
        # source will indicate target_builder's location
        if parent.file.filename == target_builder.source:
            link_obj = SoftLink(path)
        elif target_builder.source is not None:
            link_obj = ExternalLink(target_builder.source, path)
        else:
            msg = 'cannot create external link to %s' % path
            raise ValueError(msg)
        parent[name] = link_obj
        return link_obj

    @classmethod
    def isinstance_inmemory_array(cls, data):
        """Check if an object is a common in-memory data structure"""
        return isinstance(data, list) or \
               isinstance(data, np.ndarray) or \
               isinstance(data, tuple) or \
               isinstance(data, set) or \
               isinstance(data, str) or \
               isinstance(data, frozenset)

    @docval({'name': 'parent', 'type': Group, 'doc': 'the parent HDF5 object'},
            {'name': 'builder', 'type': DatasetBuilder, 'doc': 'the DatasetBuilder to write'},
            returns='the Dataset that was created', rtype=Dataset)
    def write_dataset(self, **kwargs):
        """ Write a dataset to HDF5

        The function uses other dataset-dependent write functions, e.g,
        __scalar_fill__, __list_fill__ and __chunked_iter_fill__ to write the data.
        """
        parent, builder = getargs('parent', 'builder', kwargs)
        name = builder.name
        data = builder.data
        attributes = builder.attributes
        dtype = builder.dtype
        dset = None
        link = None
        if isinstance(dtype, list):
            # do some stuff to figure out what data is a reference
            refs = list()
            for i, dts in enumerate(dtype):
                if self.__is_ref(dts):
                    refs.append(i)
            if len(refs) > 0:
                _dtype = self.__resolve_dtype__(dtype, data)
                def _filler():
                    #dset = parent.create_dataset(name, data=ref, shape=None, dtype=_dtype)
                    ret = list()
                    for item in data:
                        new_item = list(item)
                        for i in refs:
                            new_item[i] = self.__get_ref(item[i])
                        ret.append(tuple(new_item))
                    dset = parent.create_dataset(name, shape=(len(ret),), dtype=_dtype)
                    dset[:] = ret
                    self.set_attributes(dset, attributes)
                self.__queue_ref(_filler)
                return
            else:
                dset = self.__list_fill__(parent, name, data, dtype)
        else:
            if isinstance(data, str):
                dset = self.__scalar_fill__(parent, name, data)
            elif isinstance(data, DataChunkIterator):
                dset = self.__chunked_iter_fill__(parent, name, data)
            elif isinstance(data, Dataset):
                data_filename = os.path.abspath(data.file.filename)
                parent_filename = os.path.abspath(parent.file.filename)
                if data_filename != parent_filename:
                    link = ExternalLink(os.path.relpath(data_filename, os.path.dirname(parent_filename)), data.name)
                else:
                    link = SoftLink(data.name)
                parent[name] = link
            elif isinstance(data, Builder):
                _dtype = self.__dtypes[dtype]
                if dtype == 'region':
                    def _filler():
                        ref = self.__get_ref(data, builder.region)
                        dset = parent.create_dataset(name, data=ref, shape=None, dtype=_dtype)
                        self.set_attributes(dset, attributes)
                    self.__queue_ref(_filler)
                else:
                    def _filler():
                        ref = self.__get_ref(data)
                        dset = parent.create_dataset(name, data=ref, shape=None, dtype=_dtype)
                        self.set_attributes(dset, attributes)
                    self.__queue_ref(_filler)
                return
            elif isinstance(data, Iterable) and not self.isinstance_inmemory_array(data):
                dset = self.__chunked_iter_fill__(parent, name, DataChunkIterator(data=data, buffer_size=100))
            elif hasattr(data, '__len__'):
                dset = self.__list_fill__(parent, name, data, dtype_spec=dtype)
            else:
                dset = self.__scalar_fill__(parent, name, data, dtype=dtype)
        if link is None:
            self.set_attributes(dset, attributes)
        return dset

    @classmethod
    def __selection_max_bounds__(cls, selection):
        """Determine the bounds of a numpy selection index tuple"""
        if isinstance(selection, int):
            return selection+1
        elif isinstance(selection, slice):
            return selection.stop
        elif isinstance(selection, list) or isinstance(selection, np.ndarray):
            return np.nonzero(selection)[0][-1]+1
        elif isinstance(selection, tuple):
            return tuple([cls.__selection_max_bounds__(i) for i in selection])

    @classmethod
    def __scalar_fill__(cls, parent, name, data, dtype=None):
        if not isinstance(dtype, type):
            dtype = cls.__resolve_dtype__(dtype, data)
        try:
            dset = parent.create_dataset(name, data=data,shape=None, dtype=dtype)
        except Exception as exc:
            msg = "Could not create scalar dataset %s in %s" % (name, parent.name)
            raise_from(Exception(msg), exc)
        return dset

    @classmethod
    def __chunked_iter_fill__(cls, parent, name, data):
        """
        Write data to a dataset one-chunk-at-a-time based on the given DataChunkIterator

        :param parent: The parent object to which the dataset should be added
        :type parent: h5py.Group, h5py.File
        :param name: The name of the dataset
        :type name: str
        :param data: The data to be written.
        :type data: DataChunkIterator

        """
        recommended_chunks = data.recommended_chunk_shape()
        chunks = True if recommended_chunks is None else recommended_chunks
        baseshape = data.recommended_data_shape()
        try:
            dset = parent.create_dataset(name, shape=baseshape, dtype=data.dtype, maxshape=data.max_shape, chunks=chunks)
        except Exception as exc:
            raise_from(Exception("Could not create dataset %s in %s" % (name, parent.name)), exc)
        for chunk_i in data:
            # Determine the minimum array dimensions to fit the chunk selection
            max_bounds = cls.__selection_max_bounds__(chunk_i.selection)
            if not hasattr(max_bounds, '__len__'):
                max_bounds = (max_bounds,)
            # Determine if we need to expand any of the data dimensions
            expand_dims = [i for i, v in enumerate(max_bounds) if v is not None and v > dset.shape[i]]
            # Expand the dataset if needed
            if len(expand_dims) > 0:
                new_shape = np.asarray(dset.shape)
                new_shape[expand_dims] = np.asarray(max_bounds)[expand_dims]
                dset.resize(new_shape)
            # Process and write the data
            dset[chunk_i.selection] = chunk_i.data
        return dset

    @classmethod
    def __list_fill__(cls, parent, name, data, dtype_spec=None):
        if not isinstance(dtype_spec, type):
            dtype = cls.__resolve_dtype__(dtype_spec, data)
        else:
            dtype = dtype_spec
        if isinstance(dtype, np.dtype):
            data_shape = (len(data),)
        else:
            data_shape = get_shape(data)
        try:
            dset = parent.create_dataset(name, shape=data_shape, dtype=dtype)
        except Exception as exc:
            msg = "Could not create dataset %s in %s" % (name, parent.name)
            msg = "%s dtype_spec = %s" % (msg, dtype_spec)
            raise_from(Exception(msg), exc)
        if len(data) > dset.shape[0]:
            new_shape = list(dset.shape)
            new_shape[0] = len(data)
            dset.resize(new_shape)
        try:
            dset[:] = data
        except Exception as e:
            raise e
        return dset

    def __get_ref_filler(self, dset, sl, f):
        def _call():
           dset[sl] = f()
        return _call

    def __get_ref(self, container, region=None):
        if isinstance(container, Container):
            builder = self.manager.build(container)
        else:
            if isinstance(container, Builder):
                if isinstance(container, LinkBuilder):
                    builder = container.target_builder
                else:
                    builder = container
        path = self.__get_path(builder)
        if region is not None:
            dset = self.__file[path]
            if not isinstance(dset, Dataset):
                raise ValueError('cannot create region reference without Dataset')
            return self.__file[path].regionref[region]
        else:
            return self.__file[path].ref

    def __is_ref(self, dtype):
        if isinstance(dtype, DtypeSpec):
            return self.__is_ref(dtype.dtype)
        elif isinstance(dtype, RefSpec):
            return True
        else:
            return dtype == DatasetBuilder.OBJECT_REF_TYPE or dtype == DatasetBuilder.REGION_REF_TYPE

    def __queue_ref(self, func):
        '''Set aside filling dset with references

        dest[sl] = func()

        Args:
           dset: the h5py.Dataset that the references need to be added to
           sl: the np.s_ (slice) object for indexing into dset
           func: a function to call to return the chunk of data, with
                 references filled in
        '''
        self.__ref_queue.append(func)

    def __rec_get_ref(self, l):
        ret = list()
        for elem in l:
            if isinstance(elem, (list, tuple)):
                ret.append(self.__rec_get_ref(elem))
            elif isinstance(elem, (Builder, Container)):
                ret.append(self.__get_ref(elem))
            else:
                ret.append(elem)
        return ret



class H5SpecWriter(SpecWriter):

    __str_type = special_dtype(vlen=binary_type)

    @docval({'name': 'group', 'type': Group, 'doc': 'the HDF5 file to write specs to'})
    def __init__(self, **kwargs):
        self.__group = getargs('group', kwargs)

    @staticmethod
    def stringify(spec):
        '''
        Converts a spec into a JSON string to write to a dataset
        '''
        return json.dumps(spec, separators=(',', ':'))

    def __write(self, d, name):
        data = self.stringify(spec)
        dset = self.__group.create_dataset(name, data=data, dtype=self.__str_type)
        return dset

    def write_spec(self, spec, path):
        return self.__write(spec, path)

    def write_namespace(self, namespace, path):
        return self.__write({'namespaces': namespace}, path)

class H5SpecReader(SpecReader):

    @docval({'name': 'group', 'type': Group, 'doc': 'the HDF5 file to read specs from'})
    def __init__(self, **kwargs):
        self.__group = getargs('group', kwargs)

    def __read(self, path):
        s = self.__group[path][()]
        d = json.loads(s)
        return d

    def read_spec(self, spec_path):
        return self.__read(spec_path)

    def read_namespace(self, ns_path):
        return self.__read(ns_path)

