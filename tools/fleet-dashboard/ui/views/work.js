/* Fleet route module: work. */
(function registerWorkView() {
  'use strict';
  globalThis.FleetViewModules.register({
    id: 'work',
    owns: [
      'ticket work queue',
    'ticket filters',
    'work empty states'
    ],
    render() { return globalThis.renderWarmWork(); }
  });
})();
