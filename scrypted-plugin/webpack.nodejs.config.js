// Override: the published @scrypted/sdk 0.5.59's default production config enables webpack's
// module concatenation ("scope hoisting") optimization, which fails to bundle this SDK's own
// `dist/src/index.js` (a `CommonJS` re-export of `MixinDeviceBase` alongside a wildcard
// `export * from '../types/gen/index'`) with:
//   "Cannot get final name for export 'MixinDeviceBase' ... while generating the root export"
// That is a webpack/SDK interaction bug, not anything about this plugin's own code -- disabling
// just this one optimization (bundle size is not a concern for a single small plugin) avoids it
// while keeping everything else (minification included) from the SDK's own default config.
const base = require('./node_modules/@scrypted/sdk/webpack.nodejs.config.js');
base.optimization = base.optimization || {};
base.optimization.concatenateModules = false;
module.exports = base;
