/* Shared contract for independently owned Fleet route modules. */
(function installFleetViewRegistry() {
  'use strict';
  const views = new Map();
  globalThis.FleetViewModules = Object.freeze({
    register(view) {
      if (!view || typeof view.id !== 'string' || typeof view.render !== 'function') {
        throw new TypeError('Fleet view modules require id and render');
      }
      if (views.has(view.id)) throw new Error(`duplicate Fleet view module: ${view.id}`);
      views.set(view.id, Object.freeze({...view, owns: Object.freeze([...(view.owns || [])])}));
    },
    has(id) { return views.has(id); },
    render(id) {
      const view = views.get(id);
      if (!view) throw new Error(`unknown Fleet view module: ${id}`);
      return view.render();
    },
    describe() { return [...views.values()].map(({id, owns}) => ({id, owns})); }
  });
})();
