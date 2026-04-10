import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { Auth0Provider } from '@auth0/auth0-react'
import './index.css'
import App from './App.tsx'

const defaultDomain = 'dev-pzmiocjfeo5mjqn2.us.auth0.com'
const defaultAudience = 'https://uar-copilot-api'

// Domain and audience can safely default.
const auth0Domain =
  (import.meta.env.VITE_AUTH0_DOMAIN as string | undefined)?.trim() ||
  defaultDomain
const auth0Audience =
  (import.meta.env.VITE_AUTH0_AUDIENCE as string | undefined)?.trim() ||
  defaultAudience
// Client ID must come from the tenant you're actually using.
const auth0ClientId =
  (import.meta.env.VITE_AUTH0_CLIENT_ID as string | undefined)?.trim() || ''

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    {auth0ClientId ? (
      <Auth0Provider
        domain={auth0Domain}
        clientId={auth0ClientId}
        authorizationParams={{
          redirect_uri: window.location.origin,
          audience: auth0Audience,
        }}
        cacheLocation="localstorage"
        useRefreshTokens
      >
        <App />
      </Auth0Provider>
    ) : (
      <App />
    )}
  </StrictMode>,
)
