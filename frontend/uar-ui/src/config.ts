const env = import.meta.env

const trim = (v: unknown) => (typeof v === 'string' ? v.trim() : '')

export const API_BASE_URL = (
  trim(env.VITE_API_BASE_URL) || (env.PROD ? 'https://uar-copilot.onrender.com' : 'http://localhost:8000')
).replace(/\/+$/, '')

export const auth0Config = {
  domain: trim(env.VITE_AUTH0_DOMAIN),
  clientId: trim(env.VITE_AUTH0_CLIENT_ID),
  audience: trim(env.VITE_AUTH0_AUDIENCE),
}

export const authEnabled = Boolean(auth0Config.domain && auth0Config.clientId && auth0Config.audience)
