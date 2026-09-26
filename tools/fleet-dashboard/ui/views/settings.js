/* Fleet route module: settings. */
(function registerSettingsView() {
  'use strict';
  globalThis.FleetViewModules.register({
    id: 'settings',
    owns: [
      'seat configuration',
    'dispatch policy',
    'release and door controls'
    ],
    render() { return globalThis.renderWarmSettings(); }
  });
})();
