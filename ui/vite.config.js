import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    open: true,
    proxy: {
      '/api': {
        target: 'https://3en35lspt5.execute-api.us-east-1.amazonaws.com',
        changeOrigin: true,
        secure: true,
        rewrite: path => path.replace(/^\/api/, '/dev'),
      },
    },
  },
})
