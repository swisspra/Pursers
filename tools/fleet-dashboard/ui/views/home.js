/* Fleet route module: home. */
(function registerHomeView() {
  'use strict';
  globalThis.FleetViewModules.register({
    id: 'home',
    owns: [
      'fleet health summary',
    'attention queue',
    'guided start'
    ],
    render() { return globalThis.renderWarmHome(); }
  });
})();
