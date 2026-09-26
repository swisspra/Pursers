/* Fleet route module: projects. */
(function registerProjectsView() {
  'use strict';
  globalThis.FleetViewModules.register({
    id: 'projects',
    owns: [
      'project and board cards',
    'workspace links',
    'project empty states'
    ],
    render() { return globalThis.renderWarmProjects(); }
  });
})();
