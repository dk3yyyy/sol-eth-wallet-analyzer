function csvCell(value) {
  if (value === null || value === undefined) return '';
  let text = String(value);
  if (/^[=+\-@\t\r]/.test(text)) text = `'${text}`;
  return /[",\n\r]/.test(text) ? `"${text.replaceAll('"', '""')}"` : text;
}

export function snapshotCsv(result) {
  const rows = [
    ['Name', 'Symbol', 'Mint or contract', 'Balance', 'Price USD', 'Value USD', '24h %', 'Liquidity USD'],
    ['Native asset', result.native_asset.symbol, '', result.native_asset.balance, result.native_asset.price_usd, result.native_asset.value_usd, '', ''],
    ...result.tokens.map((token) => [
      token.name,
      token.symbol,
      token.mint,
      token.balance,
      token.price_usd,
      token.value_usd,
      token.price_change_24h_percent,
      token.liquidity_usd,
    ]),
  ];
  return rows.map((row) => row.map(csvCell).join(',')).join('\n');
}

export function downloadText(filename, text, contentType) {
  const blob = new Blob([text], { type: contentType });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = filename;
  document.body.append(anchor);
  anchor.click();
  anchor.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 0);
}

export function sharedAddressFromHash(hash) {
  const params = new URLSearchParams(hash.replace(/^#/, ''));
  return params.get('address')?.trim() || '';
}
