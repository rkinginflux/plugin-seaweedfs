"""Recording test double for the injected DB3 API, not an SDK implementation.

Only used by contract tests. It intentionally does not validate full line
protocol semantics. Real SDK/runtime compatibility requires a DB3 plugin test.
No database SDK or proprietary source is vendored in this repository.
"""
import json


class LineBuilder:
    def __init__(self, measurement):
        self.measurement = measurement
        self.tags = {}
        self.fields = {}
        self.timestamp = None

    def tag(self, key, value):
        self.tags[key] = value
        return self

    def string_field(self, key, value):
        self.fields[key] = json.dumps(value)
        return self

    def uint64_field(self, key, value):
        self.fields[key] = str(value) + 'u'
        return self

    def bool_field(self, key, value):
        self.fields[key] = 't' if value else 'f'
        return self

    def time_ns(self, value):
        self.timestamp = value
        return self

    def build(self):
        tags = ''.join(',' + key + '=' + value for key, value in self.tags.items())
        fields = ','.join(key + '=' + value for key, value in self.fields.items())
        return self.measurement + tags + ' ' + fields + ' ' + str(self.timestamp)
