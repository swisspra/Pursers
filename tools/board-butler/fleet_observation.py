"""Local evidence producer for the existing authorized fleet reconciler."""
import json
import os
import re
import tempfile
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path


SAFE_SEAT_ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,119}$')


def timestamp(value):
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return result if result.tzinfo else None
    except (AttributeError, TypeError, ValueError):
        return None


def publish(path, document):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix='.observation-')
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'w') as handle:
            json.dump(document, handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def ticket_holds_seat(ticket, agent):
    """Protect the exact holder; unknown active ownership protects every seat."""
    holders = []
    if ticket.get('status') in {'claimed', 'in_progress', 'reviewing'}:
        holders.append((ticket, ('claimed_by_agent_id', 'claimed_by', 'claimed_by_principal_id')))
    review = ticket.get('review_lease')
    if isinstance(review, Mapping) and review:
        holders.append((review, ('reviewer_agent_id', 'reviewer_agent_name', 'reviewer_principal_id')))
    for record, keys in holders:
        identified = False
        for key, identity in zip(keys, ('agent_id', 'agent_name', 'principal_id')):
            value = record.get(key)
            if isinstance(value, str) and value:
                identified = True
                if value == agent.get(identity):
                    return True
                break
        if not identified:
            return True
    return False


class LocalFleetObserver:
    def __init__(self, templates, services, stored, bindings):
        self.templates, self.services, self.stored, self.bindings = templates, services, stored, bindings

    def collect(self, active_boards, snapshots, memberships, now, providers, host):
        expiry = (now + timedelta(seconds=120)).isoformat()
        readiness = {'schema': 'pursers_registry_readiness_v1', 'stale_after': expiry,
                     'selected_active_boards': list(active_boards),
                     'boards': {b: {'seats': {}} for b in active_boards}}
        leases = {'boards': {b: {'seats': {}} for b in active_boards}}
        seats, health, limits = [], {b: {} for b in active_boards}, {b: {} for b in active_boards}
        for template_id, template in self.templates.items():
            binding = self.bindings.get(template_id, {})
            binding_mapping = isinstance(binding, Mapping)
            seat_id = binding.get('seat_id', template.seat_root.name) if binding_mapping else template.seat_root.name
            board_id = binding.get('board_id', next(iter(active_boards), 'unknown')) if binding_mapping else 'unknown'
            provider = binding.get('provider', 'unknown') if binding_mapping else 'unknown'
            enabled = binding.get('enabled', True) if binding_mapping else False
            binding_valid = (
                binding_mapping
                and {'board_id', 'provider'} <= set(binding)
                and set(binding) <= {'board_id', 'provider', 'enabled', 'seat_id'}
                and isinstance(seat_id, str)
                and SAFE_SEAT_ID.fullmatch(seat_id) is not None
                and type(enabled) is bool
            )
            previous = self.stored.get(seat_id, {})
            service = self.services.inspect(seat_id, template)
            known, busy = bool(active_boards), False
            for board in active_boards:
                snapshot, membership = snapshots.get(board, {}), memberships.get(board, {})
                agents, tickets = snapshot.get('agents'), snapshot.get('coordination_tickets', snapshot.get('tickets'))
                members = membership.get('members')
                if (not isinstance(agents, list) or not isinstance(tickets, list) or not isinstance(members, list)
                        or (snapshot.get('truncated') and snapshot.get('coordination_tickets_complete') is not True)
                        or membership.get('truncated') or membership.get('next_cursor')):
                    known = False
                    continue
                matches = [a for a in agents if isinstance(a, Mapping)
                           and a.get('principal_id') == template.principal_id and a.get('agent_name') == seat_id]
                member_matches = [m for m in members if isinstance(m, Mapping) and m.get('principal_id') == template.principal_id]
                if len(matches) != 1 or len(member_matches) != 1:
                    known = False
                    continue
                agent, member = matches[0], member_matches[0]
                seen = timestamp(agent.get('last_activity_at') or agent.get('last_seen'))
                fresh = seen is not None and 0 <= (now-seen).total_seconds() <= 300
                caps = agent.get('capabilities', {})
                ready = agent.get('readiness', {})
                # A stopped process cannot send heartbeats. Current registry
                # identity/membership still authorizes restart; live leases below
                # remain protected. Running processes require fresh activity.
                activity_valid = fresh or (not service.running and seen is not None and seen <= now)
                valid = (activity_valid and not (isinstance(ready, Mapping) and ready.get('reported') is True and ready.get('dispatch_ready') is not True)
                         and agent.get('lifecycle_status') == 'active'
                         and agent.get('role') == template.role
                         and member.get('role') == ('reviewer' if template.role == 'reviewer' else 'member')
                         and all(type(caps.get(k)) is type(v) and caps[k] == v for k,v in template.capabilities.items()))
                if not valid:
                    known = False
                    continue
                readiness['boards'][board]['seats'][seat_id] = {
                    'principal_id': template.principal_id, 'role': agent['role'],
                    'membership_role': member['role'], 'lifecycle_status': 'active', 'capabilities': caps}
                expiry_at = timestamp(agent.get('lease_expires_at'))
                busy |= agent.get('status') in {'busy', 'working'} or bool(expiry_at and expiry_at > now)
                # Ticket projections differ between product versions. Any active lease
                # not attributable to a holder conservatively protects every seat.
                for ticket in tickets:
                    if not isinstance(ticket, Mapping):
                        known = False
                        continue
                    if ticket_holds_seat(ticket, agent):
                        busy = True
            if known:
                for board in active_boards:
                    leases['boards'][board]['seats'][seat_id] = {'stale_after': expiry, 'work': busy, 'review': busy}
            lifecycle = ('ready' if service.ready else 'starting') if service.running else 'stopped'
            if service.running and previous.get('lifecycle') == 'draining': lifecycle = 'draining'
            if busy and lifecycle == 'ready': lifecycle = 'busy'
            if ((service.exists and not service.identity_verified) or not known
                    or not binding or not binding_valid or board_id not in active_boards): lifecycle = 'unhealthy'
            if board_id not in active_boards:
                raise ValueError('template binding targets an inactive board')
            seats.append({'seat_id': seat_id, 'board_id': board_id, 'role': template.role, 'provider': provider,
                'template_id': template_id, 'template_digest_sha256': template.digest_sha256,
                'generation': previous.get('generation', 1), 'lifecycle': lifecycle,
                'transition_at': datetime.fromtimestamp(previous.get('last_mutation', now.timestamp()), timezone.utc).isoformat(),
                'managed': enabled if binding_valid else False})
            health[board_id][provider] = providers.get(provider, {'status': 'unknown', 'latency_ms': 0})
            if enabled and binding_valid:
                limits[board_id][provider] = limits[board_id].get(provider, 0) + 1
        observation = {'schema': 'pursers_fleet_observation_v1', 'schema_version': 1,
            'observed_at': now.isoformat(), 'stale_after': expiry, 'executor_seats': seats,
            'provider_observations': health, 'provider_maximums': limits, 'host_observation': host}
        return observation, readiness, leases
