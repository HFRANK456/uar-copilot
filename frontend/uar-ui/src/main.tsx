import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { Auth0Provider } from '@auth0/auth0-react'
import './index.css'
import App from './App.tsx'

const defaultDomain = 'dev-pzmiocjfeo5mjqn2.us.auth0.com'
const defaultClientId = '1BZScmvum58uuFuMiegEMvsNMyj94754'
const defaultAudience = 'https://uar-copilot-api'

// Force known-good Auth0 values to avoid broken deploy env config.
const auth0Domain = defaultDomain
const auth0ClientId = defaultClientId
const auth0Audience = defaultAudience

createRoot(document.getElementById('root')!).render(
  <StrictMode>
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
  </StrictMode>,
)
