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
      }
    }
  },
  plugins: []
}
