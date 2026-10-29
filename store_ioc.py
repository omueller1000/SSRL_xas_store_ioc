#!/usr/bin/env python3
"""A minimal EPICS IOC exposing read write enabled PVs defined in the external file store.json"""

import json
import os
import shutil

from caproto import MAX_STRING_SIZE
from caproto.server import PVGroup, pvproperty, ioc_arg_parser, run

STORE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'store.json')

# Maps the "dtype" strings used in the store file onto Python types.
DTYPES = {
    'float': float,
    'int': int,
    'str': str,
    'bool': bool,
}

# Spellings accepted for a bool "value" given as a string in the store file.
BOOL_STRINGS = {'true': True, 'on': True, '1': True,
                'false': False, 'off': False, '0': False}


def initial_value(spec):
    """Return (dtype, value) for a store entry, rejecting values that would load wrongly."""
    pvname = spec['name']
    dtype_name = spec.get('dtype', 'float')
    if dtype_name not in DTYPES:
        raise ValueError(f'PV {pvname!r}: unknown dtype {dtype_name!r} '
                         f'(expected {", ".join(DTYPES)})')
    dtype = DTYPES[dtype_name]
    if 'value' not in spec:
        return dtype, dtype()

    raw = spec['value']

    def bad(reason):
        return ValueError(f'PV {pvname!r}: {reason}, got {raw!r}')

    if dtype is bool:
        if isinstance(raw, bool):
            return dtype, raw
        if isinstance(raw, int) and raw in (0, 1):
            return dtype, bool(raw)
        if isinstance(raw, str) and raw.strip().lower() in BOOL_STRINGS:
            return dtype, BOOL_STRINGS[raw.strip().lower()]
        raise bad('expected true/false, 0/1 or "on"/"off"')

    if dtype is str:
        if not isinstance(raw, str):
            raise bad('expected a string')
        # A CA string is MAX_STRING_SIZE bytes including the terminating null.
        if len(raw.encode()) >= MAX_STRING_SIZE:
            raise bad(f'string longer than {MAX_STRING_SIZE - 1} bytes')
        return dtype, raw

    # float / int: JSON true/false would otherwise load as 1/0.
    if isinstance(raw, bool):
        raise bad(f'expected a number for dtype {dtype_name!r}')
    if dtype is int and isinstance(raw, int):
        return dtype, raw
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise bad(f'expected a number for dtype {dtype_name!r}') from None
    if dtype is int:
        if not value.is_integer():
            raise bad('expected a whole number for dtype \'int\'')
        return dtype, int(value)
    return dtype, value


def load_store(path=STORE_FILE):
    """Read and return the store file as a dict."""
    with open(path) as f:
        return json.load(f)


def apply_port(port):
    """Point the CA server at `port` unless EPICS_CA_SERVER_PORT is already set.

    caproto reads the variable at server startup. Returns where the port in
    effect came from: 'environment', 'store.json' or 'caproto default'.
    """
    if os.environ.get('EPICS_CA_SERVER_PORT'):
        return 'environment'
    if port is not None:
        os.environ['EPICS_CA_SERVER_PORT'] = str(port)
        return 'store.json'
    return 'caproto default'


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

    # Write to a sibling temp file, fsync it, and rename, so a crash or power
    # loss mid-write cannot leave a truncated or empty store.json behind.
    tmp = f'{path}.tmp'
    with open(tmp, 'w') as f:
        json.dump(store, f, indent=2)
        f.write('\n')
        f.flush()
        os.fsync(f.fileno())
    shutil.copymode(path, tmp)
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
    if dtype is bool and isinstance(value, str):
        # caproto delivers bool PVs as their enum strings ('Off'/'On'), and
        # bool('Off') would be True.
        return value == 'On'
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
    seen = set()
    for i, spec in enumerate(pv_specs):
        pvname = spec['name']
        if pvname in seen:
            raise ValueError(f'duplicate PV name in store: {pvname!r}')
        seen.add(pvname)
        dtype, value = initial_value(spec)
        kwargs = {'precision': 15} if dtype is float else {}
        # The attribute key is synthetic so PV names cannot shadow PVGroup
        # internals (e.g. a PV called PREFIX); `name=` sets the real PV name.
        attrs[f'pv_{i}'] = pvproperty(
            value=value,
            name=pvname,
            dtype=dtype,
            doc=spec.get('description', ''),
            put=make_store_putter(pvname, dtype, path),
            **kwargs,
        )
    return type(name, (base,), attrs)


if __name__ == '__main__':
    store = load_store()
    port_source = apply_port(store.get('port'))
    StoreIOC = build_ioc_class(store.get('pvs', []))

    ioc_options, run_options = ioc_arg_parser(
        default_prefix=store.get('prefix', ''),
        desc=__doc__,
    )
    print(f"CA server port: {os.environ.get('EPICS_CA_SERVER_PORT', 5064)} "
          f'(from {port_source})')
    ioc = StoreIOC(**ioc_options)
    print('PVs:', list(ioc.pvdb))
    run(ioc.pvdb, **run_options)
