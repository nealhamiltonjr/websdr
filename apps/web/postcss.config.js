// Tailwind 4 uses CSS-first config (no JS config needed).
// Just enables the postcss plugin that processes @import "tailwindcss".
// ESM syntax (parent package.json has "type": "module").
export default {
  plugins: {
    '@tailwindcss/postcss': {},
    autoprefixer: {},
  },
};
