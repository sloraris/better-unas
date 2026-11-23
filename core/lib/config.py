"""A tool's settings: its config file, checked against its settings.json,
plus the suite's shared settings. Standard library only."""

import os

from core.lib import settings

CORE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _shared_schema():
    """The shared settings (SHARED_KEYS) as core/settings.json defines them."""
    core = settings.load_schema(os.path.join(CORE_DIR, "settings.json"))
    keep = [f for f in settings.fields(core) if f["key"] in settings.SHARED_KEYS]
    return {"contract": core.get("contract", 1),
            "groups": [{"id": "notifications", "title": "Notifications", "fields": keep}]}


SHARED_SCHEMA = _shared_schema()


class Config:
    """A tool's config file plus the suite's shared settings, each read from
    better-unas.conf when it sets the key, else from this config.

    cfg[KEY] and typed hold the parsed values (an int, a bool, a list...);
    values holds each setting's text in canonical form ("yes", "5,187"). A bad
    or unknown line is reported in errors ("KEY: message", also in
    field_errors by key) and the default is used instead.
    """

    def __init__(self, schema, path, core_path):
        self.schema = schema
        raw, problems = settings.read(path)
        self.typed, self.field_errors = settings.validate(schema, raw)
        self.errors = [f"config {p}" for p in problems]
        core, core_problems = settings.read(core_path)
        self.errors += [f"better-unas.conf {p}" for p in core_problems]
        shared_raw = {k: core[k] if k in core else raw[k]
                      for k in settings.SHARED_KEYS if k in core or k in raw}
        shared, shared_errors = settings.validate(SHARED_SCHEMA, shared_raw)
        self.typed.update(shared)
        self.field_errors.update(shared_errors)
        self.errors += [f"{k}: {m}" for k, m in self.field_errors.items()]
        self.values = {f["key"]: settings.format_value(f, self.typed[f["key"]])
                       for f in settings.fields(schema) + settings.fields(SHARED_SCHEMA)}

    def __getitem__(self, k):
        return self.typed[k]
