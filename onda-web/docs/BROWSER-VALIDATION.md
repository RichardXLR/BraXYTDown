# Validação entre navegadores e dispositivos

O workflow `onda-browser-quality.yml` executa Chromium, Firefox e WebKit em jobs separados, sem credenciais de usuários. A seleção explícita de um motor exige que ele esteja instalado: erro de inicialização falha o teste, em vez de gerar um falso resultado aprovado.

```sh
python -m pip install -r requirements.txt
python -m pip install pytest==8.4.2 playwright==1.63.0
python -m playwright install --with-deps chromium firefox webkit
ONDA_TEST_BROWSERS=chromium,firefox,webkit python -m pytest -q tests/test_session_store.py tests/test_maintenance_center_browser.py tests/test_history_storage_browsers.py tests/test_browser_media_flow.py
```

Os testes verificam criptografia e persistência dos cookies em IndexedDB, isolamento de contas, limpeza após interrupção, integridade de todos os bytes do protocolo de download, armazenamento OPFS ou fallback seguro em memória, painel histórico, sanitização e layouts com larguras de 320, 390, 768 e 1440 pixels. Falta de suporte ao OPFS não aumenta o limite do fallback em RAM.

O teste `test_browser_media_flow.py` percorre o fluxo completo em cada motor: navegador → servidor HTTP local → API com identidade Clerk de teste → FFmpeg real → protocolo de download → armazenamento do navegador → botão Salvar → arquivo no disco → histórico numérico. A sessão Clerk e o transporte Blob são fixtures explícitas. Não se substitui o transporte binário HTTP por `route.fulfill`, que pode reter o corpo binário no WebKit; somente os scripts Clerk de teste são interceptados. Os cenários cobrem MP4 em 1440 px e MP3 em 390 px, com toque habilitado na viewport móvel, sem afirmar que um aparelho físico foi testado.

**WebKit no Linux não é Safari no macOS/iOS. Uma viewport de celular não é um aparelho real.** Esta automação cobre motores e dimensões; a validação física abaixo continua necessária e não é declarada concluída sem evidência.

## Matriz física a executar

| Dispositivo | Navegador | Verificações |
| --- | --- | --- |
| iPhone atual | Safari | Login Clerk, reprodução com som, salvar arquivo no app Arquivos, rotação, cookies após fechar/reabrir |
| iPhone anterior ainda suportado | Safari | As mesmas verificações, armazenamento insuficiente, retorno após bloquear a tela |
| Android intermediário | Chrome | Login, vídeo/áudio, salvar, cancelamento, pouca memória, mudança de Wi-Fi para dados móveis |
| Android | Firefox | As mesmas verificações e fallback de armazenamento quando OPFS não estiver disponível |
| Mac | Safari | Login, cookies, áudio/vídeo, download nativo e abertura dos arquivos salvos |

Use somente `https://ondaittoux.online/canary.wav` e `/canary.mp4` ou mídia própria/autorizada. Comece com os canários pequenos, depois um arquivo próprio de 100 MiB; não apresente 500 MiB ou 1 GiB como capacidade validada da Function atual. O worker externo permanece desativado por decisão do usuário.

Para cada execução registre data, modelo, versão do sistema/navegador, tamanho real, formato, resultado e código de erro. Não inclua tokens, cookies, links temporários de entrega ou dados de outras contas. Confirme que os números surgem na aba **Desempenho** após uma execução e que desaparecem ao sair da conta. O pico de memória é uma observação cumulativa do processo do servidor, não uma medição da RAM do telefone.

Erros causados por sessão rejeitada, permissão ausente ou bloqueio legítimo da origem devem continuar visíveis. Nenhum teste remove a autenticação do site publicado.
