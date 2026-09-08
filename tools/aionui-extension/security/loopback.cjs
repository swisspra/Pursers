'use strict';

function isLoopbackHostname(hostname) {
  const normalized = String(hostname || '')
    .replace(/^\[|\]$/g, '')
    .replace(/\.$/, '')
    .toLowerCase();
  if (normalized === 'localhost' || normalized.endsWith('.localhost') || normalized === '::1') {
    return true;
  }
  if (!/^\d{1,3}(?:\.\d{1,3}){3}$/.test(normalized)) {
    return false;
  }
  const octets = normalized.split('.').map(Number);
  return octets[0] === 127 && octets.every((octet) => octet >= 0 && octet <= 255);
}

module.exports = { isLoopbackHostname };
