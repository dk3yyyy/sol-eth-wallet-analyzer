import { useMemo, useState } from 'react';
import { downloadText, sharedAddressFromHash, snapshotCsv } from './snapshot.js';

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

function portfolioComposition(result) {
  const positions = [
    {
      id: `native-${result.native_asset.symbol}`,
      label: result.native_asset.symbol,
      value: Number(result.native_asset.value_usd),
    },
    ...result.tokens.map((token) => ({
      id: token.mint,
      label: token.symbol,
      value: Number(token.value_usd),
    })),
  ]
    .filter((position) => Number.isFinite(position.value) && position.value > 0)
    .sort((left, right) => right.value - left.value);

  if (positions.length < 2) return [];
  const valuedTotal = positions.reduce((total, position) => total + position.value, 0);
  const leading = positions.slice(0, 4);
  const remainder = positions.slice(4).reduce((total, position) => total + position.value, 0);
  if (remainder > 0) leading.push({ id: 'other', label: 'Other', value: remainder });
  return leading.map((position) => ({
    ...position,
    percentage: (position.value / valuedTotal) * 100,
  }));
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

function TokenMark({ token }) {
  const [logoFailed, setLogoFailed] = useState(false);
  if (!token.logo_available || logoFailed) {
    return <span className="asset-monogram" aria-hidden="true">{token.symbol.slice(0, 2)}</span>;
  }
  return (
    <img
      className="token-logo"
      src={`/api/token-logo/${encodeURIComponent(token.mint)}`}
      alt=""
      aria-hidden="true"
      loading="lazy"
      decoding="async"
      referrerPolicy="no-referrer"
      onError={() => setLogoFailed(true)}
    />
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

function PortfolioResult({ result, loading, onRefresh, onCopyAddress, onCopyShare, onAnnounce }) {
  const [tokenQuery, setTokenQuery] = useState('');
  const [sortBy, setSortBy] = useState('value');
  const [hideDust, setHideDust] = useState(false);
  const nativeAllocation = Math.max(0, 100 - result.token_summary.allocation_percent);
  const explorerUrl = safeHttpsUrl(result.explorer_url);
  const composition = useMemo(() => portfolioComposition(result), [result]);
  const visibleTokens = useMemo(() => {
    const query = tokenQuery.trim().toLowerCase();
    return result.tokens
      .filter((token) => {
        const matchesQuery = !query
          || [token.symbol, token.name, token.mint]
            .some((value) => String(value || '').toLowerCase().includes(query));
        const isValuedDust = token.value_usd !== null
          && token.value_usd !== undefined
          && Number(token.value_usd) < 1;
        return matchesQuery && (!hideDust || !isValuedDust);
      })
      .sort((left, right) => {
        if (sortBy === 'symbol') return left.symbol.localeCompare(right.symbol);
        if (sortBy === 'balance') return Number(right.balance || 0) - Number(left.balance || 0);
        const leftValue = left.value_usd === null || left.value_usd === undefined ? -1 : Number(left.value_usd);
        const rightValue = right.value_usd === null || right.value_usd === undefined ? -1 : Number(right.value_usd);
        return rightValue - leftValue;
      });
  }, [hideDust, result.tokens, sortBy, tokenQuery]);

  function exportCsv() {
    downloadText('chainscope-holdings.csv', snapshotCsv(result), 'text/csv;charset=utf-8');
    onAnnounce('CSV export prepared.');
  }

  function exportJson() {
    downloadText(
      'chainscope-snapshot.json',
      `${JSON.stringify(result, null, 2)}\n`,
      'application/json;charset=utf-8',
    );
    onAnnounce('JSON export prepared.');
  }

  return (
    <section className="portfolio" aria-labelledby="portfolio-value">
      <header className="portfolio-header">
        <div className="wallet-identity">
          <span className={`chain-tag chain-${result.chain}`}>{chainLabel(result.chain)}</span>
          <div className="address-control">
            <p className="address-line" title={result.address}>{shortenAddress(result.address)}</p>
            <button type="button" onClick={onCopyAddress} aria-label="Copy analyzed address">Copy</button>
          </div>
        </div>
        <div className="portfolio-actions">
          <button type="button" className="secondary-action" onClick={onRefresh} disabled={loading}>
            {loading ? 'Refreshing…' : 'Refresh data'}
          </button>
          {explorerUrl && (
            <a href={explorerUrl} target="_blank" rel="noreferrer" className="text-link">
              {explorerLabel(result.chain)} <span aria-hidden="true">↗</span>
            </a>
          )}
        </div>
      </header>

      <div className="value-block">
        <span>Estimated portfolio value</span>
        <h2 id="portfolio-value">{formatCurrency(result.total_value_usd)}</h2>
        <p>Read-only snapshot · Updated {new Date(result.updated_at).toLocaleString()}</p>
      </div>

      <div className="snapshot-actions" aria-label="Snapshot actions">
        <p>The address in a share link stays in the URL fragment and is not analyzed until submitted.</p>
        <div>
          <button type="button" onClick={onCopyShare}>Copy share link</button>
          <button type="button" onClick={exportCsv}>Export CSV</button>
          <button type="button" onClick={exportJson}>Export JSON</button>
        </div>
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

        {composition.length > 0 && (
          <section className="asset-composition" aria-labelledby="composition-title">
            <div className="composition-heading">
              <div>
                <span className="eyebrow">VALUED ASSETS</span>
                <h4 id="composition-title">Top asset allocation</h4>
              </div>
              <span>Unpriced holdings excluded</span>
            </div>
            <ol>
              {composition.map((position) => (
                <li key={position.id}>
                  <span className="composition-label">
                    <strong>{position.label}</strong>
                    <small>{position.percentage.toFixed(1)}%</small>
                  </span>
                  <progress
                    max="100"
                    value={position.percentage}
                    aria-label={`${position.label} ${position.percentage.toFixed(1)} percent`}
                  />
                  <span className="composition-value">{formatCurrency(position.value)}</span>
                </li>
              ))}
            </ol>
          </section>
        )}
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
            <div className="holdings-toolbar">
              <label>
                <span>Search token holdings</span>
                <input
                  type="search"
                  value={tokenQuery}
                  onChange={(event) => setTokenQuery(event.target.value)}
                  placeholder="Name, symbol, or mint"
                />
              </label>
              <label>
                <span>Sort holdings</span>
                <select value={sortBy} onChange={(event) => setSortBy(event.target.value)}>
                  <option value="value">Value: high to low</option>
                  <option value="balance">Balance: high to low</option>
                  <option value="symbol">Symbol: A–Z</option>
                </select>
              </label>
              <label className="dust-toggle">
                <input
                  type="checkbox"
                  checked={hideDust}
                  onChange={(event) => setHideDust(event.target.checked)}
                />
                <span>Hide holdings under $1</span>
              </label>
            </div>
            <p className="holdings-count">Showing {visibleTokens.length} of {result.tokens.length} holdings</p>
            {visibleTokens.length > 0 ? (
              <>
                <p className="table-scroll-hint">Swipe the table to see price and market data →</p>
                <div className="table-wrap">
                  <table role="table" aria-label="Token holdings table">
              <thead role="rowgroup">
                <tr role="row">
                  <th scope="col" role="columnheader">Asset</th>
                  <th scope="col" role="columnheader">Balance</th>
                  <th scope="col" role="columnheader">Price</th>
                  <th scope="col" role="columnheader">Value</th>
                  <th scope="col" role="columnheader">24h</th>
                  <th scope="col" role="columnheader">Liquidity</th>
                </tr>
              </thead>
              <tbody role="rowgroup">
                {visibleTokens.map((token) => {
                  const marketUrl = safeHttpsUrl(token.market_url);
                  return (
                    <tr key={token.mint} role="row">
                      <td data-label="Asset" role="cell">
                        <div className="asset-cell">
                          <TokenMark token={token} />
                          <div>
                            <strong>{token.symbol}</strong>
                            <small>{token.name}</small>
                          </div>
                          {marketUrl && <a href={marketUrl} target="_blank" rel="noreferrer" aria-label={`View ${token.symbol} market`}>↗</a>}
                        </div>
                      </td>
                      <td data-label="Balance" role="cell">{formatQuantity(token.balance)}</td>
                      <td data-label="Price" role="cell">{formatCurrency(token.price_usd, 6)}</td>
                      <td data-label="Value" role="cell"><strong>{formatCurrency(token.value_usd)}</strong></td>
                      <td data-label="24h" role="cell" className={Number(token.price_change_24h_percent) >= 0 ? 'positive' : 'negative'}>
                        {token.price_change_24h_percent === null ? '—' : `${Number(token.price_change_24h_percent) >= 0 ? '+' : ''}${formatQuantity(token.price_change_24h_percent, 2)}%`}
                      </td>
                      <td data-label="Liquidity" role="cell">{token.liquidity_usd === null ? '—' : `$${compactNumber(token.liquidity_usd)}`}</td>
                    </tr>
                  );
                })}
              </tbody>
                  </table>
                </div>
              </>
            ) : (
              <p className="empty-holdings">No holdings match the current filters.</p>
            )}
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

function initialSharedAddress() {
  const candidate = sharedAddressFromHash(window.location.hash);
  return ethereumPattern.test(candidate) || solanaPattern.test(candidate) ? candidate : '';
}

export default function App() {
  const [address, setAddress] = useState(initialSharedAddress);
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

  async function runAnalysis(normalized, forceRefresh = false) {
    setLoading(true);
    setError('');
    setStatus(forceRefresh ? 'Refreshing public on-chain data.' : 'Analyzing public on-chain data.');
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), 25_000);

    try {
      const payload = forceRefresh
        ? { address: normalized, force_refresh: true }
        : { address: normalized };
      const response = await fetch('/api/analyze', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
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
      setStatus(forceRefresh && result
        ? 'Refresh failed. Previous snapshot retained.'
        : 'Wallet analysis failed.');
    } finally {
      window.clearTimeout(timeout);
      setLoading(false);
    }
  }

  async function handleSubmit(event) {
    event.preventDefault();
    const normalized = address.trim();
    if (!ethereumPattern.test(normalized) && !solanaPattern.test(normalized)) {
      setError('Enter a valid Solana or Ethereum wallet address.');
      setStatus('Wallet address validation failed.');
      return;
    }

    await runAnalysis(normalized);
  }

  async function handleRetry() {
    const normalized = address.trim();
    if (loading || (!ethereumPattern.test(normalized) && !solanaPattern.test(normalized))) return;
    await runAnalysis(normalized);
  }

  async function handleRefresh() {
    if (!result || loading) return;
    await runAnalysis(result.address, true);
  }

  async function handleCopyShare() {
    if (!result) return;
    const shareUrl = `${window.location.origin}${window.location.pathname}#address=${encodeURIComponent(result.address)}`;
    try {
      await navigator.clipboard.writeText(shareUrl);
      setError('');
      setStatus('Share link copied. Opening it pre-fills the public address but does not analyze automatically.');
    } catch {
      setError('The share link could not be copied. Check browser clipboard permissions.');
      setStatus('Share link copy failed.');
    }
  }

  async function handleCopyAddress() {
    if (!result) return;
    try {
      await navigator.clipboard.writeText(result.address);
      setError('');
      setStatus('Wallet address copied.');
    } catch {
      setError('The wallet address could not be copied. Check browser clipboard permissions.');
      setStatus('Wallet address copy failed.');
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
            {error && (
              <div className="form-error" role="alert">
                <span>{error}</span>
                {(ethereumPattern.test(address.trim()) || solanaPattern.test(address.trim())) && (
                  <button type="button" onClick={handleRetry} disabled={loading}>Retry analysis</button>
                )}
              </div>
            )}
          </form>
        </section>

        {loading && (
          <section className="loading-panel" aria-label="Analysis in progress" aria-busy="true">
            <span className="eyebrow">READING PUBLIC DATA</span>
            <strong>{result ? 'Refreshing the latest snapshot…' : 'Building the wallet snapshot…'}</strong>
            <div aria-hidden="true"><i /><i /><i /></div>
          </section>
        )}

        {result ? (
          <PortfolioResult
            result={result}
            loading={loading}
            onRefresh={handleRefresh}
            onCopyAddress={handleCopyAddress}
            onCopyShare={handleCopyShare}
            onAnnounce={setStatus}
          />
        ) : (
          <InitialPanel />
        )}
      </main>

      <footer>
        <span>ChainScope / Read-only blockchain intelligence</span>
        <span>Market values are estimates, not financial advice.</span>
      </footer>
      <p className="sr-only" role="status" aria-live="polite">{status}</p>
    </div>
  );
}
