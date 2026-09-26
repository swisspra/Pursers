/* Fleet route module: projects. */
(function registerProjectsView() {
  'use strict';
  let esc, pageHead, warmTruthStrip, warmBoards, numberCount, boardHref;
  function useContext(context) {
    ({esc, pageHead, warmTruthStrip, warmBoards, numberCount, boardHref} = context);
  }
  function renderWarmProjects(){const cards=warmBoards().map(({central,board})=>{const counts=board.counts||{},total=Object.values(counts).reduce((sum,value)=>sum+numberCount(value),0),truncation=board.snapshot_truncation,limited=truncation&&truncation.total>truncation.returned;return `<article class="board-card" data-board-id="${esc(board.board_id)}" data-central="${esc(central)}" data-pursers-board="${esc(board.board_id)}" data-pursers-status="${esc(board.status||'unknown')}"><div><p class="eyebrow">${esc(central)}</p><h3>${esc(board.label)}</h3><p class="meta">${esc(board.board_id)} · ${esc(total)} visible tickets${limited?` · bounded ${esc(truncation.returned)} of ${esc(truncation.total)}`:''}</p></div><div class="counts">${Object.entries(counts).map(([key,value])=>`<span class="pill">${esc(key.replaceAll('_',' '))} <b>${esc(value)}</b></span>`).join('')}</div><div class="card-actions"><a class="primary-action" href="${boardHref(central,board.board_id)}">Open project</a><a href="${boardHref(central,board.board_id,'routes')}">Routes</a></div></article>`}).join('');return `${pageHead('Projects','Connected projects','Choose a project, check its connection health, or add another from Settings.')}${warmTruthStrip()}<section class="boards-list">${cards||'<div class="empty-guidance"><h3>No connected projects</h3><p>Add one project from Settings to create its board and protected connection.</p><div class="card-actions"><a class="primary-action" href="#/settings">Add project</a></div></div>'}</section>`}
  globalThis.FleetViewModules.register({
    id: 'projects',
    owns: [
      'project and board cards',
    'workspace links',
    'project empty states'
    ],
    render(context) { useContext(context); return renderWarmProjects(); }
  });
})();
