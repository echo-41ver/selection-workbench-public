// Route only this collector's browser. A process-level DIRECT choice does not
// disable an operating-system VPN/TUN and does not establish its public exit IP.
export function amazonBrowserRoute(environment = process.env) {
  const mode = String(environment.AMAZON_BROWSER_ROUTE_MODE || 'AUTO').trim().toUpperCase();
  if (!['AUTO', 'DIRECT'].includes(mode)) throw new Error('Amazon browser route must be AUTO or DIRECT');
  const server = environment.AMAZON_BROWSER_PROXY;
  return {
    launchOptions: mode === 'DIRECT'
      ? {args: ['--no-proxy-server']}
      : server ? {proxy: {server}} : {},
    evidence: {
      mode,
      transport: mode === 'DIRECT' ? 'BROWSER_DIRECT' : server ? 'CONFIGURED_PROXY' : 'SYSTEM_ROUTE',
      egress_verified: false,
    },
  };
}
