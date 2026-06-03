// Neutral palette is driven through CSS variables (channel triplets, so
// opacity modifiers like bg-slate-900/40 and variants like hover:bg-slate-50
// keep working) to enable dark mode without touching markup. Light values in
// :root match Tailwind's defaults exactly (zero light-mode regression); the
// dark overrides live under [data-theme="dark"]. See src/index.css.
// NOTE: `white` is intentionally NOT tokenized — text-white must stay literal
// white on coloured surfaces. Dark card surfaces use an explicit .bg-white
// override in index.css instead.
const slate = (n) => `rgb(var(--c-slate-${n}) / <alpha-value>)`

export default {
  content: ["./index.html", "./src/**/*.{js,jsx}"],
  theme: {
    extend: {
      colors: {
        brand: { 50: '#f0f4ff', 500: '#3b5bdb', 700: '#2f4ac0', 900: '#1a2d7a' },
        critical: '#dc2626',
        high: '#ea580c',
        medium: '#ca8a04',
        low: '#16a34a',
        slate: {
          50:  slate(50),  100: slate(100), 200: slate(200), 300: slate(300),
          400: slate(400), 500: slate(500), 600: slate(600), 700: slate(700),
          800: slate(800), 900: slate(900),
        },
      }
    }
  },
  plugins: []
}
