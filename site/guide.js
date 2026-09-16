// Progressive enhancement only: all instructions and navigation are static HTML.
const status = document.querySelector('#guide-copy-status');
let statusTimer;
if (navigator.clipboard?.writeText) {
  for (const block of document.querySelectorAll('.guide-code')) {
    const code = block.querySelector('code');
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'guide-copy';
    button.textContent = 'Copy';
    button.setAttribute('aria-label', `Copy ${block.dataset.label}`);
    block.querySelector('.guide-code-label').append(button);
    button.addEventListener('click', async () => {
      clearTimeout(statusTimer);
      try {
        await navigator.clipboard.writeText(code.textContent);
        status.textContent = 'Copied. Review your paths and placeholders before running.';
      } catch {
        status.textContent = 'Clipboard unavailable. Select and copy the text directly.';
      }
      statusTimer = setTimeout(() => { status.textContent = ''; }, 5000);
    });
  }
}
