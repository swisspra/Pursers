/* Fleet route module: activity. */
(function registerActivityView() {
  'use strict';
  globalThis.FleetViewModules.register({
    id: 'activity',
    owns: [
      'recent activity',
    'butler activity',
    'activity empty states'
    ],
    render() { return globalThis.renderWarmActivity() + globalThis.renderAutonomousActivity(); }
  });
})();
