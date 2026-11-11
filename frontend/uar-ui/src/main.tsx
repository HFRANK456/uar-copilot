import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { Auth0Provider, type AppState } from '@auth0/auth0-react'
import { BrowserRouter } from 'react-router-dom'
import './index.css'
import App from './App.tsx'
import { auth0Config, authEnabled } from './config.ts'

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    {authEnabled ? (
      <Auth0Provider
        domain={auth0Config.domain}
        clientId={auth0Config.clientId}
        authorizationParams={{
          redirect_uri: window.location.origin,
          audience: auth0Config.audience,
        }}
        cacheLocation="localstorage"
        useRefreshTokens
        onRedirectCallback={(appState?: AppState) => {
          // Return to the page the user signed in from.
          window.history.replaceState({}, '', appState?.returnTo ?? window.location.pathname)
          window.dispatchEvent(new PopStateEvent('popstate'))
        }}
      >
        <BrowserRouter>
          <App />
        </BrowserRouter>
      </Auth0Provider>
    ) : (
      <BrowserRouter>
        <App />
      </BrowserRouter>
    )}
  </StrictMode>,
)
