/* Fleet route module: team. */
(function registerTeamView() {
  'use strict';
  globalThis.FleetViewModules.register({
    id: 'team',
    owns: [
      'agent roster',
    'seat filters',
    'autonomous butler status'
    ],
    render() { return globalThis.renderAgentsHub() + globalThis.renderAutonomousTeam(); }
  });
})();
