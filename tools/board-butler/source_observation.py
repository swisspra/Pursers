"""Declared, bounded source counts. Unknown observations never mean no work."""
import asyncio
import copy
import json
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class SourceObservationPolicy:
    read_tool: str
    arguments: Mapping
    count_path: str
    max_age_s: int = 120

    @classmethod
    def from_mapping(cls, value, tools):
        if not isinstance(value, Mapping) or set(value) - {'read_tool', 'arguments', 'count_path', 'max_age_s'}:
            raise ValueError('source.observation is malformed')
        tool = value.get('read_tool')
        if not any(t.name == tool and t.effect == 'read_only' for t in tools):
            raise ValueError('source.observation.read_tool must be declared read_only')
        args, path, age = value.get('arguments'), value.get('count_path'), value.get('max_age_s', 120)
        if not isinstance(args, Mapping) or len(json.dumps(args).encode()) > 16384:
            raise ValueError('source.observation.arguments must be bounded')
        if not isinstance(path, str) or not re.fullmatch(r'[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+){0,15}', path):
            raise ValueError('source.observation.count_path is invalid')
        if type(age) is not int or not 1 <= age <= 120:
            raise ValueError('source.observation.max_age_s must be between 1 and 120')
        return cls(tool, copy.deepcopy(dict(args)), path, age)


@dataclass(frozen=True)
class SourceCountObservation:
    source_id: str
    count: int | None
    status: str
    observed_at: str
    error_class: str | None = None

    def decision_fields(self):
        return {'open_issue_count': self.count, 'observed_at': self.observed_at,
                'observation_error': self.error_class}


async def observe_source(source_id, policy, read_tool, now, *, clock=time.monotonic):
    if policy is None:
        return SourceCountObservation(source_id, None, 'unavailable', now.isoformat(), 'Unconfigured')
    started = clock()
    try:
        document = await asyncio.wait_for(read_tool(policy.read_tool, dict(policy.arguments)), policy.max_age_s)
        for part in policy.count_path.split('.'):
            if not isinstance(document, Mapping) or part not in document:
                raise ValueError('missing count')
            document = document[part]
        if type(document) is not int or document < 0:
            raise ValueError('invalid count')
        if clock() - started >= policy.max_age_s:
            raise TimeoutError('stale count')
        return SourceCountObservation(source_id, document, 'ok', now.isoformat())
    except Exception as exc:
        # Exception messages can contain credentials or untrusted upstream bodies.
        return SourceCountObservation(source_id, None, 'unavailable', now.isoformat(), type(exc).__name__[:80])
