// Tailwind config for the compiled, vendored stylesheet.
// Mirrors the theme the dashboard was designed with; regenerate the CSS
// with scripts/build-css.sh after changing this file or the template.
module.exports = {
  content: ["./templates/**/*.html"],
  theme: {
    extend: {
      fontFamily: {
        display: ['"Chakra Petch"', 'sans-serif'],
        mono: ['"IBM Plex Mono"', 'monospace'],
      },
      colors: {
        ink:    '#05080a',
        panel:  '#0a0f12',
        edge:   'rgba(125, 245, 175, 0.10)',
        fg:     '#dce7e1',
        dim:    '#71837c',
        signal: '#4df59c',
        amber:  '#ffb454',
        alert:  '#ff5f56',
      },
    },
  },
};
