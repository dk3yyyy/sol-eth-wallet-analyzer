import { useMemo, useState } from 'react';

const ethereumPattern = /^0x[a-fA-F0-9]{40}$/;
const solanaPattern = /^[1-9A-HJ-NP-Za-km-z]{32,44}$/;

function formatCurrency(value, maximumFractionDigits = 2) {
  if (value === null || value === undefined || !Number.isFinite(Number(value))) return '—';
  return new Intl.NumberFormat('en-US', {
    style: 'currency',
    currency: 'USD',
    maximumFractionDigits,
  }).format(Number(value));
}

function formatQuantity(value, maximumFractionDigits = 6) {
  if (value === null || value === undefined || !Number.isFinite(Number(value))) return '—';
  return new Intl.NumberFormat('en-US', {
    maximumFractionDigits,
  }).format(Number(value));
}

function compactNumber(value) {
  if (value === null || value === undefined || !Number.isFinite(Number(value))) return '—';
  return new Intl.NumberFormat('en-US', {
    notation: 'compact',
    maximumFractionDigits: 1,
  }).format(Number(value));
}

function shortenAddress(address) {
  if (!address || address.length < 18) return address;
  return `${address.slice(0, 8)}…${address.slice(-7)}`;
}

function chainLabel(chain) {
  return chain === 'solana' ? 'Solana' : 'Ethereum';
}

function explorerLabel(chain) {
  return chain === 'solana' ? 'View on Solscan' : 'View on Etherscan';
}

function safeHttpsUrl(value) {
  try {
    const parsed = new URL(value);
    return parsed.protocol === 'https:' ? parsed.href : null;
  } catch {
    return null;
  }
}

function BrandMark() {
  return (
    <svg viewBox="0 0 40 40" aria-hidden="true" className="brand-mark">
      <path d="M8 11.5 14.5 5h17L25 11.5H8Z" />
      <path d="m8 17 6.5-6.5h17L25 17H8Z" opacity=".72" />
      <path d="m8 28.5 6.5 6.5h17L25 28.5H8Z" opacity=".42" />
    </svg>
  );
}

function ArrowIcon() {
  return (
    <svg viewBox="0 0 20 20" aria-hidden="true">
      <path d="M4 10h11M11 6l4 4-4 4" fill="none" stroke="currentColor" strokeWidth="1.7" />
    </svg>
  );
}

function InitialPanel() {
  return (
    <section className="capabilities" aria-labelledby="capabilities-title">
      <div className="section-heading">
        <span className="eyebrow">01 / COVERAGE</span>
        <h2 id="capabilities-title">One address. The useful signals.</h2>
      </div>
      <div className="capability-grid">
        <article>
          <span className="capability-index">A</span>
          <h3>Native balance</h3>
          <p>Current SOL or ETH balance with its live USD valuation.</p>
        </article>
        <article>
          <span className="capability-index">B</span>
          <h3>Token exposure</h3>
          <p>Valued Solana holdings, sorted by position size and market context.</p>
        </article>
        <article>
          <span className="capability-index">C</span>
          <h3>Data confidence</h3>
          <p>Missing market data stays visible instead of becoming a false zero.</p>
        </article>
      </div>
    </section>
  );
}

function PortfolioResult({ result }) {
  const nativeAllocation = Math.max(0, 100 - result.token_summary.allocation_percent);
  const explorerUrl = safeHttpsUrl(result.explorer_url);

  return (
    <section className="portfolio" aria-labelledby="portfolio-value">
      <header className="portfolio-header">
        <div>
          <span className={`chain-tag chain-${result.chain}`}>{chainLabel(result.chain)}</span>
          <p className="address-line" title={result.address}>{shortenAddress(result.address)}</p>
        </div>
        {explorerUrl && (
          <a href={explorerUrl} target="_blank" rel="noreferrer" className="text-link">
            {explorerLabel(result.chain)} <span aria-hidden="true">↗</span>
          </a>
        )}
      </header>

      <div className="value-block">
        <span>Estimated portfolio value</span>
        <h2 id="portfolio-value">{formatCurrency(result.total_value_usd)}</h2>
        <p>Read-only snapshot · Updated {new Date(result.updated_at).toLocaleString()}</p>
      </div>

      {result.warnings.length > 0 && (
        <div className="warning-band" role="note">
          <strong>Partial market coverage</strong>
          <span>{result.warnings.join(' ')}</span>
        </div>
      )}

      <div className="metric-grid">
        <article>
          <span>Native asset</span>
          <strong>{formatQuantity(result.native_asset.balance)} {result.native_asset.symbol}</strong>
          <small>{formatCurrency(result.native_asset.value_usd)}</small>
        </article>
        <article>
          <span>Native price</span>
          <strong>{formatCurrency(result.native_asset.price_usd)}</strong>
          <small>Current provider price</small>
        </article>
        <article>
          <span>Valued tokens</span>
          <strong>{result.token_summary.valued_count}</strong>
          <small>{result.token_summary.holding_count} detected</small>
        </article>
        <article>
          <span>Market coverage</span>
          <strong>
            {result.token_summary.holding_count === 0
              ? 'Native only'
              : `${result.token_summary.valued_count}/${result.token_summary.holding_count}`}
          </strong>
          <small>{result.token_summary.unavailable_count} unavailable</small>
        </article>
      </div>

      <section className="allocation" aria-labelledby="allocation-title">
        <div className="section-row">
          <h3 id="allocation-title">Portfolio allocation</h3>
          <span>{formatCurrency(result.total_value_usd)}</span>
        </div>
        <progress
          className="allocation-track"
          max="100"
          value={nativeAllocation}
          aria-label={`Native assets ${nativeAllocation.toFixed(1)}%, tokens ${result.token_summary.allocation_percent.toFixed(1)}%`}
        >
          {nativeAllocation.toFixed(1)}% native assets
        </progress>
        <div className="allocation-legend">
          <span><i className="native-dot" />{result.native_asset.symbol} <b>{nativeAllocation.toFixed(1)}%</b></span>
          <span><i className="token-dot" />Tokens <b>{result.token_summary.allocation_percent.toFixed(1)}%</b></span>
        </div>
      </section>

      <section className="holdings" aria-labelledby="holdings-title">
        <div className="section-row">
          <div>
            <span className="eyebrow">02 / POSITIONS</span>
            <h3 id="holdings-title">Token holdings</h3>
          </div>
          <span>{formatCurrency(result.token_summary.value_usd)}</span>
        </div>
        {result.tokens.length > 0 ? (
          <>
            <p className="table-scroll-hint">Swipe the table to see price and market data →</p>
            <div className="table-wrap">
              <table>
              <thead>
                <tr>
                  <th scope="col">Asset</th>
                  <th scope="col">Balance</th>
                  <th scope="col">Price</th>
                  <th scope="col">Value</th>
                  <th scope="col">24h</th>
                  <th scope="col">Liquidity</th>
                </tr>
              </thead>
              <tbody>
                {result.tokens.map((token) => {
                  const marketUrl = safeHttpsUrl(token.market_url);
                  return (
                    <tr key={token.mint}>
                      <td>
                        <div className="asset-cell">
                          <span className="asset-monogram" aria-hidden="true">{token.symbol.slice(0, 2)}</span>
                          <div>
                            <strong>{token.symbol}</strong>
                            <small>{token.name}</small>
                          </div>
                          {marketUrl && <a href={marketUrl} target="_blank" rel="noreferrer" aria-label={`View ${token.symbol} market`}>↗</a>}
                        </div>
                      </td>
                      <td>{formatQuantity(token.balance)}</td>
                      <td>{formatCurrency(token.price_usd, 6)}</td>
                      <td><strong>{formatCurrency(token.value_usd)}</strong></td>
                      <td className={Number(token.price_change_24h_percent) >= 0 ? 'positive' : 'negative'}>
                        {token.price_change_24h_percent === null ? '—' : `${Number(token.price_change_24h_percent) >= 0 ? '+' : ''}${formatQuantity(token.price_change_24h_percent, 2)}%`}
                      </td>
                      <td>{token.liquidity_usd === null ? '—' : `$${compactNumber(token.liquidity_usd)}`}</td>
                    </tr>
                  );
                })}
              </tbody>
              </table>
            </div>
          </>
        ) : (
          <p className="empty-holdings">
            {result.chain === 'ethereum'
              ? 'Ethereum token holdings are not indexed in this release. Native ETH is fully valued above.'
              : 'No valued token positions were found for this address.'}
          </p>
        )}
      </section>
    </section>
  );
}

export default function App() {
  const [address, setAddress] = useState('');
  const [result, setResult] = useState(null);
  const [error, setError] = useState('');
  const [status, setStatus] = useState('');
  const [loading, setLoading] = useState(false);

  const detectedChain = useMemo(() => {
    const candidate = address.trim();
    if (ethereumPattern.test(candidate)) return 'Ethereum address';
    if (solanaPattern.test(candidate)) return 'Solana address';
    return null;
  }, [address]);

  async function handleSubmit(event) {
    event.preventDefault();
    const normalized = address.trim();
    if (!ethereumPattern.test(normalized) && !solanaPattern.test(normalized)) {
      setError('Enter a valid Solana or Ethereum wallet address.');
      setStatus('Wallet address validation failed.');
      return;
    }

    setLoading(true);
    setError('');
    setStatus('Analyzing public on-chain data.');
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), 25_000);

    try {
      const response = await fetch('/api/analyze', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ address: normalized }),
        signal: controller.signal,
      });
      const body = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(body.detail || 'Analysis could not be completed.');
      setResult(body);
      const coverageNotice = body.warnings?.length
        ? ` Partial market coverage: ${body.warnings.join(' ')}`
        : '';
      setStatus(`${chainLabel(body.chain)} wallet analysis complete.${coverageNotice}`);
    } catch (requestError) {
      const message = requestError.name === 'AbortError'
        ? 'Analysis timed out. Try again shortly.'
        : requestError.message;
      setError(message);
      setStatus('Wallet analysis failed.');
    } finally {
      window.clearTimeout(timeout);
      setLoading(false);
    }
  }

  return (
    <div className="app-shell">
      <header className="site-header">
        <a href="/" className="brand" aria-label="ChainScope home">
          <BrandMark />
          <span><strong>ChainScope</strong><small>Wallet intelligence</small></span>
        </a>
        <div className="header-meta">
          <span><i className="status-dot" /> Live public data</span>
          <span className="read-only">Read-only</span>
        </div>
      </header>

      <main>
        <section className="hero" aria-labelledby="hero-title">
          <div className="hero-copy">
            <span className="eyebrow">SOLANA + ETHEREUM / NO CONNECTION REQUIRED</span>
            <h1 id="hero-title">A clear view of any wallet.</h1>
            <p>Inspect balances, token exposure, and market coverage from a public address—without connecting a wallet or signing a message.</p>
          </div>

          <form className="analyzer-form" onSubmit={handleSubmit} noValidate>
            <label htmlFor="wallet-address">Wallet address</label>
            <div className={`input-frame ${error ? 'has-error' : ''}`}>
              <span className="input-prefix" aria-hidden="true">⌁</span>
              <input
                id="wallet-address"
                name="address"
                type="text"
                value={address}
                onChange={(event) => setAddress(event.target.value)}
                placeholder="Paste a Solana or Ethereum address"
                autoComplete="off"
                autoCapitalize="none"
                spellCheck="false"
                aria-describedby="wallet-help"
              />
              {detectedChain && <span className="detected-chain">{detectedChain}</span>}
              <button type="submit" disabled={loading}>
                <span>{loading ? 'Analyzing…' : 'Analyze wallet'}</span>
                {!loading && <ArrowIcon />}
              </button>
            </div>
            <div className="form-meta">
              <p id="wallet-help">Public address only. Never enter a seed phrase or private key.</p>
              <span>No sign-in · No tracking · No storage</span>
            </div>
            {error && <p className="form-error" role="alert">{error}</p>}
          </form>
        </section>

        {result ? <PortfolioResult result={result} /> : <InitialPanel />}
      </main>

      <footer>
        <span>ChainScope / Read-only blockchain intelligence</span>
        <span>Market values are estimates, not financial advice.</span>
      </footer>
      <p className="sr-only" role="status" aria-live="polite">{status}</p>
    </div>
  );
}
