"""Native source counts must gate model requests, including after restart."""
import asyncio
from datetime import timedelta
from types import SimpleNamespace

import pytest

from test_managed_intake import Board, PagedClient, _declaration, _poller, _runtime, _source
from test_source_intake import NOW, Model, butler


@pytest.mark.parametrize('count', [0, 1, None, False, -1, '0'])
def test_native_observation_drives_resident_without_private_adapter(count):
    async def scenario():
        class Client(PagedClient):
            async def call_tool(self, name, arguments, **kwargs):
                self.calls.append((name, arguments))
                return Model(structured_content={'issues': [], 'paging': {'total': count}})
        calls = []
        runtime = _runtime(_declaration(), Client({}, calls))
        contexts = []
        # A new resident per run also proves empty-source behavior after restart.
        for restart in range(2):
            backend = butler.CentralBackend(SimpleNamespace(), 'unused')
            async def decide(context):
                contexts.append(context)
                if count == 0 and type(count) is int:
                    return await backend._source_intake_decide(context)
                return {'pull': 0}
            async def ceiling(_):
                return 15
            sources = [_source(source_id=s, observation={
                'read_tool': 'fetch', 'arguments': {'page': 1}, 'count_path': 'paging.total',
            }) for s in ('a', 'b')]
            poller = _poller(Board(), runtime, sources, ceiling=ceiling, decide=decide)
            for tick in range(60):
                result = await poller.run_cycle(NOW + timedelta(minutes=restart*60+tick))
                if type(count) is int and count == 0:
                    assert result['decision']['reason'] == 'no_open_issues'
                    assert result['decision']['model_called'] is False
        observed = contexts[-1]['sources']
        assert [s['open_issue_count'] for s in observed] == ([count]*2 if type(count) is int and count >= 0 else [None]*2)
        assert len(calls) == 240
    asyncio.run(scenario())


def test_observation_tool_must_be_declared_read_only():
    with pytest.raises(butler.ConnectorConfigError, match='read_only'):
        _poller(Board(), _runtime(_declaration(), PagedClient({}, [])), [_source(observation={
            'read_tool': 'pr_create', 'arguments': {}, 'count_path': 'paging.total',
        })])


@pytest.mark.parametrize('mode', ['missing', 'error', 'stale', 'unconfigured'])
def test_unknown_observations_never_report_zero(mode):
    async def scenario():
        policy = butler.SourceObservationPolicy('fetch', {}, 'paging.total') if mode != 'unconfigured' else None
        async def read(_tool, _args):
            if mode == 'error':
                raise RuntimeError('upstream secret must not be exposed')
            return {} if mode == 'missing' else {'paging': {'total': 0}}
        ticks = iter([0, 121])
        result = await butler.observe_source('a', policy, read, NOW, clock=lambda: next(ticks))
        assert result.count is None and result.status == 'unavailable'
        assert 'secret' not in str(result)
    asyncio.run(scenario())


def test_mixed_counts_keep_nonempty_work_visible():
    async def scenario():
        class Client(PagedClient):
            async def call_tool(self, name, arguments, **kwargs):
                return Model(structured_content={'paging': {'total': arguments['page'] - 1}})
        seen = []
        async def decide(context):
            seen.append(context)
            return {'pull': 0}
        async def ceiling(_):
            return 15
        runtime = _runtime(_declaration(), Client({}, []))
        sources = [_source(source_id=str(n), observation={
            'read_tool': 'fetch', 'arguments': {'page': n}, 'count_path': 'paging.total',
        }) for n in (1, 2)]
        await _poller(Board(), runtime, sources, ceiling=ceiling, decide=decide).run_cycle(NOW)
        assert [s['open_issue_count'] for s in seen[0]['sources']] == [0, 1]
    asyncio.run(scenario())
