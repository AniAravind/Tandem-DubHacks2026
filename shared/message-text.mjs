export function plainText(value = '') {
  // iMessage text messages do not interpret Markdown. Preserve the content,
  // especially signed URLs, while giving headings and tables readable lines.
  const input = String(value).replace(/\r\n?/g, '\n').split('\n');
  const lines = [];
  let headers = null;
  for (const raw of input) {
    const trimmed = raw.trim();
    if (/^\|.*\|$/.test(trimmed)) {
      const cells = trimmed.slice(1, -1).split('|').map(c => c.trim());
      if (cells.every(c => /^:?-{2,}:?$/.test(c))) continue;
      if (headers === null) { headers = cells; continue; }
      const numbered = /^\d+$/.test(cells[0]);
      const titleIndex = numbered ? 1 : 0;
      lines.push(`${numbered ? cells[0] + '. ' : ''}${cells[titleIndex]}`);
      cells.forEach((c, i) => { if (i > titleIndex && c) lines.push(`${headers[i] || 'Details'}: ${c}`); });
      lines.push('');
      continue;
    }
    headers = null;
    lines.push(raw.replace(/^\s{0,3}#{1,6}\s+/, '').replace(/^\s*>\s?/, '').replace(/^\s*[-*]\s+/, '• '));
  }
  return lines.join('\n')
    .replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, '$1\n$2')
    .split(/(https?:\/\/[^\s]+)/g).map(part => /^https?:\/\//.test(part) ? part : part
      .replace(/\*\*([^*\n]+)\*\*/g, '$1').replace(/__([^_\n]+)__/g, '$1')
      .replace(/`([^`\n]+)`/g, '$1')).join('').replace(/\n{3,}/g, '\n\n').trim();
}

