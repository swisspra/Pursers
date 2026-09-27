/* Fleet route module: team. */
(function registerTeamView() {
  'use strict';
  if (typeof document !== 'undefined' && !document.querySelector('link[data-fleet-route-style="team"]')) {
    const stylesheet = document.createElement('link');
    stylesheet.rel = 'stylesheet';
    stylesheet.href = '/ui/views/team.css';
    stylesheet.dataset.fleetRouteStyle = 'team';
    document.head.append(stylesheet);
  }
  let esc, pageHead, fleetData, hubWorkers, hubSeatInventory, centralLabels,
    defaultCentral, agentIdentity, agentMatchesFilters,
    agentDisplayState, workerForAgent, liveAgentCard, renderGuide, agentCountStrip,
    agentFilterBar, agentPoolScope, inactiveAgentDrawer, autonomousRows,
    autonomousStateLabel, autonomousObservationLabel;
  function useContext(context) {
    ({esc, pageHead, fleetData, hubWorkers, hubSeatInventory, centralLabels,
      defaultCentral, agentIdentity, agentMatchesFilters,
      agentDisplayState, workerForAgent, liveAgentCard, renderGuide, agentCountStrip,
      agentFilterBar, agentPoolScope, inactiveAgentDrawer, autonomousRows,
      autonomousStateLabel, autonomousObservationLabel} = context);
  }
function renderAgentsHub(){const records=[],seen=new Set();for(const [central,d] of Object.entries(fleetData)){for(const a of d.agents||[]){const key=`${central}/${agentIdentity(a)}/${a.agent_name||''}`;if(!seen.has(key))records.push({central,agent:a});seen.add(key)}}for(const [central,d] of Object.entries(hubWorkers)){const live=fleetData[central]?.agents||[];for(const w of d.workers||[]){const stable=Boolean(w.agent_id||w.principal_id),represented=live.some(a=>agentIdentity(a)===agentIdentity(w));if(represented||(!stable&&live.some(a=>a.agent_name===w.name)))continue;const work=w.current_work||[],a={agent_name:w.name,agent_id:w.agent_id,principal_id:w.principal_id,pool_status:work.length?'busy':'offline',boards:[...new Set(work.map(x=>x.board_id).filter(Boolean))],seats:[],last_seen:w.last_seen||null},key=`${central}/${agentIdentity(a)}/${a.agent_name||''}`;if(!seen.has(key))records.push({central,agent:a});seen.add(key)}}const visible=records.filter(agentMatchesFilters),cards=visible.sort((a,b)=>{const rank={working:0,available:1,connected:2,stale:3,offline:4},as=agentDisplayState(a.agent,workerForAgent(a.central,a.agent)),bs=agentDisplayState(b.agent,workerForAgent(b.central,b.agent));return rank[as]-rank[bs]||String(a.agent.agent_name||'').localeCompare(String(b.agent.agent_name||''))||a.central.localeCompare(b.central)||agentIdentity(a.agent).localeCompare(agentIdentity(b.agent))}).map(x=>liveAgentCard(x.central,x.agent).replace('<article class="agent-card ','<article class="agent-card team-roster-card ').replace('<div class="agent-board-list">','<span class="team-cost meta">Cost unknown</span><div class="agent-board-list">')),empty=records.length===0?'No seats exist in the covered boards.':'No seats match the selected filters.',scope=agentPoolScope(),unknownModel=Object.values(fleetData).reduce((total,d)=>total+Number(d.pool_summary?.unknown_model||0),0),action='<div class="agent-actions"><button id="new-agent" class="primary-action" type="button">+ New agent</button></div>',filterMarkup=agentFilterBar(records),activeFilters=(filterMarkup.match(/ selected/g)||[]).length,existingFilters=typeof document==='undefined'?null:document.querySelector('.team-filter-disclosure'),narrow=typeof matchMedia==='function'&&matchMedia('(max-width: 720px)').matches,filtersOpen=existingFilters?existingFilters.open:!narrow;return`<div class="agents-hub team-roster">${pageHead('Team','Unified agent pool','Status and ownership first; source-backed runtime details follow.',action)}${renderGuide()}<section class="strip agent-model-summary" aria-label="Model attribution coverage"><div class="metric"><span>Unknown model</span><b>${esc(unknownModel)}</b></div></section>${agentCountStrip(records)}<details class="team-filter-disclosure" data-state-key="team-filters" ${filtersOpen?'open':''}><summary>Filters${activeFilters?` · ${activeFilters} active`:''}</summary>${filterMarkup}</details>${scope}<p id="hub-agent-status" class="muted">Showing ${cards.length} of ${records.length} seats</p><section class="agent-grid dense-agent-grid team-roster-grid" data-pursers-panel="agents" data-pursers-state="${cards.length?'ready':'empty'}" aria-label="Agent roster">${cards.join('')||`<p class="empty">${esc(empty)}</p>`}</section>${inactiveAgentDrawer()}</div>`}

  const ACP_HOSTS = new Set([
    'codex', 'codex-cli', 'goose', 'claude-code', 'claude-desktop'
  ]);

  function configuredSeatForAgent(central, agent) {
    const matches = (hubSeatInventory?.seats || []).filter(
      seat => seat.name === agent.agent_name
    );
    if (matches.length !== 1) return null;
    const seat = matches[0];
    if (seat.agent_id && agent.agent_id) {
      return seat.agent_id === agent.agent_id ? seat : null;
    }
    if (seat.principal_id && agent.principal_id) {
      return seat.principal_id === agent.principal_id ? seat : null;
    }
    const liveMatches = (fleetData[central]?.agents || []).filter(
      live => live.agent_name === agent.agent_name
    );
    return liveMatches.length <= 1 ? seat : null;
  }

  function configuredSeatBoards(central, seat) {
    if (seat.boards === 'home') return seat.home_board ? [seat.home_board] : [];
    if (seat.boards && seat.boards !== 'registry') {
      return [...new Set(String(seat.boards).split(',').filter(Boolean))];
    }
    const scope = fleetData[central]?.pool_scope || {};
    const covered = scope.covered_boards || [];
    if (covered.length) return [...new Set(covered)];
    return [...new Set((fleetData[central]?.boards || []).map(board => board.board_id).filter(Boolean))];
  }

  function configuredSeatAgent(central, seat) {
    const boards = configuredSeatBoards(central, seat);
    const capabilities = {
      tier_max: seat.tier_max,
      host: seat.host,
      model: seat.model,
      provider: seat.provider,
      can_work: seat.can_work,
      can_review: seat.can_review,
      skills: seat.skills || []
    };
    return {
      agent_name: seat.name,
      agent_id: seat.agent_id,
      principal_id: seat.principal_id,
      pool_status: 'offline',
      lifecycle_status: 'active',
      boards,
      seats: boards.map(board_id => ({
        board_id,
        role: seat.role,
        capabilities
      })),
      capabilities,
      board_scope: seat.boards === 'registry' ? {
        mode: 'registry',
        status: boards.length ? 'full' : 'partial',
        active_boards: boards,
        missing_boards: boards.length ? [] : ['not observed']
      } : undefined,
      last_seen: null,
      configured_offline: true
    };
  }

  function teamHostContract(central, agent) {
    const seat = configuredSeatForAgent(central, agent);
    const resident = workerForAgent(central, agent);
    const declared = String(seat?.host_mode || seat?.runtime_mode || '').toLowerCase();
    if (declared === 'persistent' || declared === 'resident') {
      return {mode: 'persistent', seat, resident, source: 'seat inventory'};
    }
    if (declared === 'acp' || declared === 'session' || declared === 'interactive') {
      return {mode: 'acp', seat, resident: null, source: 'seat inventory'};
    }
    if (resident) return {mode: 'persistent', seat, resident, source: 'managed API worker'};
    if (seat?.host === 'headless') {
      return {mode: 'persistent', seat, resident: null, source: 'headless seat inventory'};
    }
    if (seat && ACP_HOSTS.has(seat.host)) {
      return {mode: 'acp', seat, resident: null, source: `${seat.host} seat inventory`};
    }
    return {mode: 'external', seat, resident: null, source: 'Central observation only'};
  }

  function lifecyclePanel(central, agent) {
    const contract = teamHostContract(central, agent);
    const state = agentDisplayState(agent, contract.resident);
    const plan = contract.seat
      ? '<a class="button" href="#/settings" data-team-seat-plan>Plan seat changes</a>'
      : '<span class="meta">No local seat inventory; lifecycle changes are unavailable here.</span>';
    let label, ownership, connect, process;
    if (contract.mode === 'persistent') {
      label = 'Persistent resident';
      ownership = contract.resident
        ? 'Fleet manages this resident process.'
        : 'The seat is inventoried, but Fleet has no managed process record.';
      connect = state === 'offline'
        ? 'Rejoin with the same principal and seat identity after the runtime is started.'
        : 'The resident is connected independently from its Central membership.';
      process = contract.resident
        ? 'Test, Start, Stop, and Restart control only the resident process. Stop keeps membership and history.'
        : 'Start, Stop, and Restart are unavailable until an authorized process manager owns this runtime.';
    } else if (contract.mode === 'acp') {
      label = 'Interactive ACP session';
      ownership = 'The external ACP host owns this session; Fleet does not own its process.';
      connect = state === 'offline'
        ? 'Open the host and rejoin with the same principal and seat identity.'
        : 'Closing or restarting the host makes the seat offline without retiring it.';
      process = 'Start, Stop, and Restart happen in the ACP host, not in Fleet.';
    } else {
      label = 'External host · type not reported';
      ownership = 'Central reports the seat, but no local host ownership record is available.';
      connect = 'Reconnect through the owning host with the same principal and seat identity.';
      process = 'Fleet cannot start, stop, restart, or remove an unowned host process.';
    }
    return `<details class="team-lifecycle" data-team-host-mode="${esc(contract.mode)}"><summary>${esc(label)} · lifecycle & ownership</summary><div class="team-lifecycle-grid"><p><b>Source</b><span>${esc(contract.source)}</span></p><p><b>Ownership</b><span>${esc(ownership)}</span></p><p><b>Connect / rejoin</b><span>${esc(connect)}</span></p><p><b>Process controls</b><span>${esc(process)}</span></p><p><b>Create or update</b><span>Status, role, boards, tier, model, and provider change only through an authorized plan and explicit confirmation.</span></p><p><b>Remove</b><span>Removal requires an authorized plan and preserves repositories, worktrees, credentials, logs, tickets, journal, and backups.</span></p></div><div class="team-lifecycle-actions">${plan}</div></details>`;
  }

  function teamAgentCard(central, agent) {
    const configured = configuredSeatForAgent(central, agent);
    let card = liveAgentCard(central, agent)
      .replace('<article class="agent-card ', '<article class="agent-card team-roster-card ')
      .replace('<div class="agent-board-list">', '<span class="team-cost meta">Cost unknown</span><div class="agent-board-list">');
    if (configured) {
      card = card.replace(
        '<span class="meta">Live pool seat · not locally managed</span>',
        '<span class="meta">Configured seat · process authority follows the host type below.</span>'
      );
    }
    return card.replace('</article>', `${lifecyclePanel(central, agent)}</article>`);
  }

  function renderLifecycleAgentsHub() {
    const records = [], seen = new Set();
    for (const [central, data] of Object.entries(fleetData)) {
      for (const agent of data.agents || []) {
        const key = `${central}/${agentIdentity(agent)}/${agent.agent_name || ''}`;
        if (!seen.has(key)) records.push({central, agent});
        seen.add(key);
      }
    }
    for (const [central, data] of Object.entries(hubWorkers)) {
      const live = fleetData[central]?.agents || [];
      for (const worker of data.workers || []) {
        const stable = Boolean(worker.agent_id || worker.principal_id);
        const represented = live.some(agent => agentIdentity(agent) === agentIdentity(worker));
        if (represented || (!stable && live.some(agent => agent.agent_name === worker.name))) continue;
        const work = worker.current_work || [];
        const agent = {
          agent_name: worker.name,
          agent_id: worker.agent_id,
          principal_id: worker.principal_id,
          pool_status: work.length ? 'busy' : 'offline',
          boards: [...new Set(work.map(item => item.board_id).filter(Boolean))],
          seats: [],
          last_seen: worker.last_seen || null
        };
        const key = `${central}/${agentIdentity(agent)}/${agent.agent_name || ''}`;
        if (!seen.has(key)) records.push({central, agent});
        seen.add(key);
      }
    }
    const configuredCentrals = centralLabels || [];
    const inventoryCentral = configuredCentrals.includes(defaultCentral)
      ? defaultCentral
      : configuredCentrals[0] || Object.keys(fleetData)[0] || 'default';
    for (const seat of hubSeatInventory?.seats || []) {
      if (records.some(record => record.central === inventoryCentral && record.agent.agent_name === seat.name)) continue;
      const agent = configuredSeatAgent(inventoryCentral, seat);
      const key = `${inventoryCentral}/${agentIdentity(agent)}/${agent.agent_name || ''}`;
      if (!seen.has(key)) records.push({central: inventoryCentral, agent});
      seen.add(key);
    }
    const visible = records.filter(agentMatchesFilters);
    const cards = visible.sort((a, b) => {
      const rank = {working: 0, available: 1, connected: 2, stale: 3, offline: 4};
      const aState = agentDisplayState(a.agent, workerForAgent(a.central, a.agent));
      const bState = agentDisplayState(b.agent, workerForAgent(b.central, b.agent));
      return rank[aState] - rank[bState]
        || String(a.agent.agent_name || '').localeCompare(String(b.agent.agent_name || ''))
        || a.central.localeCompare(b.central)
        || agentIdentity(a.agent).localeCompare(agentIdentity(b.agent));
    }).map(record => teamAgentCard(record.central, record.agent));
    const empty = records.length === 0
      ? 'No seats exist in the covered boards.'
      : 'No seats match the selected filters.';
    const scope = agentPoolScope();
    const unknownModel = Object.values(fleetData).reduce(
      (total, data) => total + Number(data.pool_summary?.unknown_model || 0), 0
    );
    const action = '<div class="agent-actions"><a class="primary-action" href="#/settings" data-team-new-seat>+ Plan new seat</a><button id="new-agent" type="button">Add persistent runtime</button></div>';
    const filterMarkup = agentFilterBar(records);
    const activeFilters = (filterMarkup.match(/ selected/g) || []).length;
    const existingFilters = typeof document === 'undefined'
      ? null
      : document.querySelector('.team-filter-disclosure');
    const narrow = typeof matchMedia === 'function' && matchMedia('(max-width: 720px)').matches;
    const filtersOpen = existingFilters ? existingFilters.open : !narrow;
    return `<div class="agents-hub team-roster">${pageHead('Team','Unified agent pool','Status and ownership first; source-backed runtime details follow.',action)}${renderGuide()}<section class="strip agent-model-summary" aria-label="Model attribution coverage"><div class="metric"><span>Unknown model</span><b>${esc(unknownModel)}</b></div></section>${agentCountStrip(records)}<details class="team-filter-disclosure" data-state-key="team-filters" ${filtersOpen?'open':''}><summary>Filters${activeFilters?` · ${activeFilters} active`:''}</summary>${filterMarkup}</details>${scope}<p id="hub-agent-status" class="muted">Showing ${cards.length} of ${records.length} seats</p><section class="agent-grid dense-agent-grid team-roster-grid" data-pursers-panel="agents" data-pursers-state="${cards.length?'ready':'empty'}" aria-label="Agent roster">${cards.join('')||`<p class="empty">${esc(empty)}</p>`}</section>${inactiveAgentDrawer()}</div>`;
  }
  function renderAutonomousTeam(){const cards=autonomousRows().filter(row=>row.data?.config).map(({central,board,data})=>{const desired=data.config.desired?.capacity||{},actual=data.actual_state_available?data.actual_state?.capacity||{}:{},roles=['worker','reviewer','acp_worker'],observation=autonomousObservationLabel(data);return `<article class="card" data-pursers-autonomous-team="${esc(board.board_id)}"><div class="section-title"><div><p class="eyebrow">${esc(board.label)} · ${esc(central)}</p><h3>Desired versus actual</h3></div><span class="status">${esc(autonomousStateLabel(data.effective_state))}</span></div><p class="meta" data-autonomous-observation="${esc(data.actual_state_status||'unavailable')}">Actual observation ${esc(observation)}</p><div class="autonomous-capacity">${roles.map(role=>{const target=desired[role]?.target??0,observed=actual[role],running=observed?(observed.ready||0)+(observed.busy||0):null;return `<div><b>${esc(role.replace('_',' '))}</b><p>Desired ${esc(target)}</p><span class="meta">Actual ${running===null?esc(observation):esc(running)}${observed?` · starting ${esc(observed.starting||0)} · unhealthy ${esc(observed.unhealthy||0)}`:''}</span></div>`}).join('')}</div><p class="meta">Host ${esc(data.config.host_runtime?.agent_process_ceiling||'—')} agent processes · ${esc(data.config.host_runtime?.total_process_ceiling||'—')} total ceiling · board concurrency ${esc(data.config.desired?.board_concurrency||'—')}</p></article>`}).join('');return `<section class="autonomous-grid" aria-label="Autonomous Butler fleet state">${cards||'<article class="card"><h3>Autonomous capacity</h3><p class="empty">No provisioned autonomous board state.</p></article>'}</section>`}
  globalThis.FleetViewModules.register({
    id: 'team',
    owns: [
      'agent roster',
    'seat filters',
    'autonomous butler status'
    ],
    render(context) { useContext(context); return renderLifecycleAgentsHub() + renderAutonomousTeam(); }
  });
})();
