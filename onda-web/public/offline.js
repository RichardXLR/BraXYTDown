'use strict';
document.getElementById('offline-retry').addEventListener('click', () => location.reload());
addEventListener('online', () => { document.getElementById('offline-message').textContent = 'A conexão voltou. Toque em Tentar novamente para abrir o Onda.'; });
