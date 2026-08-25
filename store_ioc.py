#!/usr/bin/env python3
"""A minimal EPICS IOC exposing read write enabled PVs defined in the external file store.json"""

import json
import os

from caproto.server import PVGroup, pvproperty, ioc_arg_parser, run

STORE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'store.json')

# Maps the "dtype" strings used in the store file onto Python types.
DTYPES = {
    'float': float,
    'int': int,
    'str': str,
    'bool': bool,
}


def load_store(path=STORE_FILE):
    """Read and return the store file as a dict."""
    with open(path) as f:
        return json.load(f)


def apply_port(port):
    """Point the CA server at `port`; caproto reads this at server startup."""
    if port is not None:
        os.environ['EPICS_CA_SERVER_PORT'] = str(port)


def save_value(pvname, value, path=STORE_FILE):
    """Write `value` into the store entry for `pvname`, leaving the rest intact."""
    with open(path) as f:
        store = json.load(f)

    for spec in store.get('pvs', []):
        if spec['name'] == pvname:
            if spec.get('value') == value:
                return False
            spec['value'] = value
            break
    else:
        return False

    # Write to a sibling temp file and rename, so a crash mid-write cannot
    # leave a truncated store.json behind.
    tmp = f'{path}.tmp'
    with open(tmp, 'w') as f:
        json.dump(store, f, indent=2)
        f.write('\n')
    os.replace(tmp, path)
    return True


def to_python(value, dtype):
    """Coerce a caproto put payload into a plain JSON-serialisable value."""
    if hasattr(value, 'tolist'):  # numpy array or scalar
        value = value.tolist()
    if isinstance(value, (list, tuple)):
        if len(value) != 1:
            return [to_python(item, dtype) for item in value]
        value = value[0]
    if isinstance(value, bytes):
        value = value.decode()
    return dtype(value)


def make_store_putter(pvname, dtype, path):
    """Build a putter that mirrors every accepted write back into the store file."""

    async def putter(group, instance, value):
        value = to_python(value, dtype)
        print(f'{instance.pvname} <- {value}')
        save_value(pvname, value, path)
        return value

    return putter


def build_ioc_class(pv_specs, path=STORE_FILE, base=PVGroup, name='StoreIOC'):
    """Build a PVGroup subclass carrying one pvproperty per entry in pv_specs."""
    attrs = {}
    for spec in pv_specs:
        pvname = spec['name']
        dtype = DTYPES[spec.get('dtype', 'float')]
        kwargs = {'precision': 15} if dtype is float else {}
        attrs[pvname.lower()] = pvproperty(
            value=dtype(spec.get('value', 0)),
            name=pvname,
            dtype=dtype,
            doc=spec.get('description', ''),
            put=make_store_putter(pvname, dtype, path),
            **kwargs,
        )
    return type(name, (base,), attrs)


if __name__ == '__main__':
    store = load_store()
    apply_port(store.get('port'))
    StoreIOC = build_ioc_class(store.get('pvs', []))

    ioc_options, run_options = ioc_arg_parser(
        default_prefix=store.get('prefix', ''),
        desc=__doc__,
    )
    print('CA server port:', os.environ.get('EPICS_CA_SERVER_PORT', 5064))
    ioc = StoreIOC(**ioc_options)
    print('PVs:', list(ioc.pvdb))
    run(ioc.pvdb, **run_options)
