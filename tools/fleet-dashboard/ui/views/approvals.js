/* Fleet route module: approvals. */
(function registerApprovalsView() {
  'use strict';
  globalThis.FleetViewModules.register({
    id: 'approvals',
    owns: [
      'approval queue',
    'human requests',
    'approval empty states'
    ],
    render() { return globalThis.renderWarmApprovals(); }
  });
})();
