# Auditoria e modernização do Onda — 11/10/2026

## Problemas reais corrigidos

A versão publicada limitava a origem a 128 MiB e a saída a 100 MiB, acumulava o arquivo em RAM no navegador, recodificava vídeos quando a estimativa ultrapassava 99% do antigo limite, escrevia uma cópia inteira de streams de transporte mesmo sem metadados DVB, não limitava a espera do resolvedor DNS e encurtava `Retry-After` para três segundos. Redirecionamentos 307/308 podiam manter o corpo de um POST para outra origem. A entrega de arquivos sem protocolo de progresso não verificava o prazo do Guard.

Esses pontos foram corrigidos diretamente no código. Limites de recursos passam a ser definidos pelo servidor e verificados contra o espaço livre. O prazo absoluto também abrange autenticação, admissão, processamento e entrega. Backpressure existe na escrita ASGI, no protocolo de frames e na gravação do navegador. Slots de consulta ficam separados dos downloads. A admissão limita uma tarefa por conta, duas por instância, e reconhece envios duplicados; metadados públicos têm cache limitado a 128 registros/60 segundos, excluindo sessões e URLs com sinais de credenciais. O circuit breaker considera falhas transitórias, incluindo tentativas esgotadas, sem compartilhar a indisponibilidade entre contas.

## Componentes modificados

- `api/resources.py`, `api/operations.py`, `api/index.py`: recursos, espaço compartilhado por instância, admissão, idempotência temporária, cache, métricas locais, cancelamento e deadline de entrega.
- `api/security.py`, `api/direct_transfer.py`, `api/recovery.py`: DNS em pool limitado, proteção de corpos de POST, validação de redirecionamentos, backoff exponencial com jitter, `Retry-After`, Range/If-Range e checkpoints HTTP.
- `api/engine.py`, `api/player.py`, `api/compatibility.py`: limites dinâmicos, remux compatível sem recodificação por tamanho, cache de probe por identidade do arquivo, sanitização de TS sob demanda, validação de áudio produzido e remoção do limite artificial da prévia.
- `api/jobs.py`, `api/worker.py`, `scripts/run_worker.py`, `Dockerfile.worker`: fila SQLite WAL opcional, persistência criptografada, prioridade, leases/fencing, bloqueio de escrita, retomada, SHA-256, entrega Range/ETag e expiração.
- `api/auth.py`, `public/account.js`, `public/cookie-center.js`, `public/session-store.js`, `public/app.js`: tratamento de indisponibilidade de JWKS, uma renovação oficial de token Clerk em 401 antes do trabalho, pré-validação da sessão da origem e avisos persistentes de renovação sem expor valores.
- `public/download-storage.js`, `public/download-stream.js`, `public/download-estimate.js`, `public/index.html`: OPFS com buffers de 1 MiB, isolamento por conta, limpeza, arquivo File apoiado em disco, salvamento por streaming, ETA baseada em medidas e retomada da tela de tarefas persistentes.
- `scripts/autocura.py`, `.github/workflows/onda-update-watch.yml`: monitor de versões por hora, detecção de dependências estáveis compatíveis, respeito à quarentena e acionamento do pipeline existente somente quando necessário.

## Medições reproduzíveis

As mídias são próprias. O benchmark de áudio usa WAV PCM real; a origem HTTP é substituída por um transporte local de arquivo de teste. As transferências, FFmpeg e verificações são reais, mas **a taxa medida é de disco local, não da Internet nem da Vercel**. Cada caso executa em um processo separado. A baseline é o commit publicado `85c03b9`. O teste atualizado usa o orçamento do worker persistente, em máquina com disco suficiente. Bytes de saída e samples PCM foram conferidos, com SHA-256 registrado.

| Entrada nominal | Baseline | Saída atual em bytes | Preparação atual | Pico RSS Python |
|---|---|---:|---:|---:|
| 16 MiB | Concluído | 16,777,122 | 0.51 s | 49.2 MiB |
| 100 MiB | Concluído | 104,857,506 | 1.12 s | 49.1 MiB |
| 250 MiB | source_too_large | 262,143,906 | 4.51 s | 49.2 MiB |
| 500 MiB | source_too_large | 524,287,906 | 6.94 s | 49.2 MiB |
| 1025 MiB | source_too_large | 1,074,790,306 | 7.31 s | 49.1 MiB |

O caso de 1.025 MiB produziu **1.074.790.306 bytes**, acima de 1 GiB. Não houve crescimento proporcional da RAM Python. O benchmark captura CPU e pico RSS do subprocesso separadamente. O tempo acima cobre transferência e preparação; a conferência de samples/hash é adicional.

Um vídeo H.264 próprio em 3840×2160, 24 fps e 2 segundos, com preenchimento CBR, ocupou 114.002.039 bytes. Antes: 2,38 s e 4,08 s de CPU FFmpeg, com recodificação. Depois: 0,27 s e 0,24 s de CPU FFmpeg, preservando o hash dos pacotes de vídeo. É uma reprodução específica do gargalo de tamanho, não uma promessa universal de ganho. O pico do subprocesso neste teste chegou a aproximadamente 472 MiB; analisar vídeo 4K também consome memória.

No decoder Node, 96 MiB passaram de cerca de 245 MiB adicionais de RSS para 57 MiB com um Blob apoiado em disco. O decoder recebeu e verificou até 1.025 MiB. O teste Node valida protocolo e armazenamento de arquivo, não compatibilidade de todos os navegadores.

No Chromium real, OPFS recebeu 100, 250 e 500 MiB em 1,42 s, 4,12 s e 5,41 s, respectivamente, com conferência de todos os bytes. São frames gerados localmente, sem tempo de rede. O cancelamento, arquivo incompleto e troca de conta foram recusados e a limpeza deixou zero arquivos temporários nesses casos. O fluxo completo local de interface → API autenticada → FFmpeg → OPFS → botão de salvar passou em 1440 px e 390 px, sem erros JS e sem overflow; Clerk e Blob eram fixtures explícitas de teste, não contas reais.

Os testes cobrem expiração de sessão, falhas HTTP, concorrência, remux/conversão, cancelamento, worker interrompido, checkpoint e verificação de integridade. Leases antigos não podem publicar resultados após cancelamento ou retomada. Um checkpoint HTTP de 8 MiB foi retomado por Range, descartando apenas o trecho sem checkpoint. A atualização conserva os testes de contas, Blob privado, cookies, interface, formatos, player e AutoCura.

Reprodução: `python -m pytest`, `scripts/benchmark_pipeline.py`, `scripts/benchmark_video.py`, `scripts/benchmark_decoder.cjs` e `scripts/verify_download_storage.py`. Os resultados brutos desta execução estão em `artifacts/modernization/` no workspace; esse diretório não é publicado nem incluído no runtime.

## AutoCura e atualização contínua

O workflow existente continua com verificação completa diária às 05:23 UTC, publicação de candidato imutável em ambiente de produção, preparado com `--skip-domain`, canários, rollback e quarentena. O novo **Onda Component Watch** consulta os componentes e a resolução das dependências a cada hora, no minuto 17, e aciona esse mesmo pipeline se houver uma atualização compatível ou promoção pendente. Nenhum componente é trocado em um processo em execução. A consulta inicial encontrou novas resoluções para `pycryptodomex`, `pydantic` e `pydantic-core`.

As versões vêm do PyPI oficial, os wheels têm integridade verificada e dependências ficam com hashes. FFmpeg segue a distribuição verificada `imageio-ffmpeg`; uma nova versão upstream que ainda não esteja no pacote não é instalada a partir de um binário arbitrário. Préreleases e versões incompatíveis não são promovidas. Agendas GitHub podem sofrer atrasos; atualização significa detecção periódica e aprovação em testes, não troca instantânea sem validação.

## Limites e dependências reais

A Function atual está configurada para **300 segundos**. A aplicação reserva 240 segundos para trabalho e usa prazo absoluto de 290 segundos, incluindo entrega. Streaming evita carregar a resposta inteira em RAM e o limite de resposta bufferizada; ele não elimina o prazo de execução. O orçamento inline conservador é **448 MiB de arquivos coexistentes**, com reserva de disco de 64 MiB. Entrada e saída coexistem durante a conversão: 250 MiB de entrada mais 250 MiB de saída não cabem nesse orçamento. A memória do subprocesso também depende do codec, da resolução e dos filtros.

O perfil worker admite 4 GiB por origem/saída, 8 GiB de área de trabalho e 30 minutos por tentativa, condicionado ao espaço real. **O worker não foi provisionado em um serviço externo.** Sua ativação requer um servidor sempre ativo e volume persistente, conforme [WORKER.md](WORKER.md). Nenhum serviço pago foi criado. O site só seleciona a fila quando configuração e heartbeat são confirmados; a rota existente continua funcionando.

Navegadores sem OPFS têm fallback de RAM de 100 MiB; o link de arquivo do worker suporta arquivos maiores sem buffering JS. O sistema de arquivos do navegador também tem cotas e pode ser apagado pelo navegador. Safari, Firefox e aparelhos reais não tiveram todos esses tamanhos testados. Em links convencionais, o navegador não expõe um evento confiável de conclusão de salvamento; a interface informa o encaminhamento, enquanto File System Access confirma a escrita quando suportado.

Checkpoints de fontes já adquiridas são reutilizados quando SHA-256 confere. A retomada de uma transferência direta exige representação estável e validators fortes. Fragmentos incompletos de todas as plataformas não têm retomada persistente garantida. Uma sessão da origem não é renovada por mecanismos não oficiais. Cookies locais permanecem cifrados até remoção explícita; no modo worker, a solicitação com cookies é temporariamente cifrada no volume e apagada ao chegar a um estado terminal. Logs nunca contêm seus valores.

Login, permissões, DRM, conteúdo removido, restrições regionais, bloqueios de IP e limites da origem continuam sendo respeitados. O bloqueio conhecido do YouTube é apresentado como bloqueado, não como teste de extração aprovado. Redis e armazenamento de objetos não foram adicionados porque a instalação de um servidor não precisa deles; para múltiplos servidores será necessário armazenamento compartilhado/filas distribuídas, mantendo as mesmas garantias de idempotência e isolamento.

Validação final local: **1.236 testes aprovados**, incluindo as verificações Chromium, antes da publicação. O pipeline também executa a suíte contra as dependências candidatas e testes reais na Vercel; o resultado da execução publicada deve ser consultado no GitHub Actions.
