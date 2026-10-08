# Onda · áudio e vídeo por link

Site em português publicado em **https://onda-audio.vercel.app**, com extração por link usando yt-dlp, FFmpeg e Deno na Vercel. Exige uma conta Clerk para usar o estúdio e suas APIs. Não usa API paga de extração. O uso gratuito depende das cotas do plano Vercel Hobby; tráfego e processamento têm limites.

## Interface e player

O estúdio mantém a identidade azul e a logo fornecida, com seis abas: download, biblioteca, compatibilidade, ajuda, personalização e créditos. A ordem do formulário é player, link, botão de download, seleção de áudio/vídeo e ajustes. O player começa oculto e entra com uma animação suave de ondas quando há uma prévia compatível. Fontes sem suporte ou com erro de mídia escondem o player e exibem o aviso junto ao link. Links reconhecidos podem usar incorporações oficiais de YouTube, TikTok, Vimeo, Dailymotion, Twitch, Facebook, Instagram e Bilibili. Outros links podem oferecer reprodução direta de mídia progressiva compatível com o navegador, com vídeo e áudio ou somente áudio.

Uma plataforma pode impedir incorporações, exigir uma sessão ou fornecer apenas faixas separadas/protegidas. Nesses casos, o site explica a indisponibilidade e mantém um link para a fonte. O catálogo de extratores não garante reprodução ou download de todos os conteúdos. Reprodução e download são operações distintas: um player oficial pode funcionar mesmo quando o servidor de extração está bloqueado. Prévia automática não envia os cookies opcionais do formulário; a tentativa explícita pode utilizá-los apenas durante aquela requisição. URLs extraídas de reprodução não ficam no histórico nem nas preferências.

A personalização oferece azul, ciano ou violeta, tema escuro/claro/do sistema, densidade, animações e abertura. Preferências, estado dos sons, abertura já vista, rascunho do formulário e os cinco links recentes são sincronizados automaticamente com a conta em Vercel Blob privado. O cache local usa um espaço separado por conta e preserva alterações pendentes; revisões e ETags impedem sobrescritas entre dispositivos. Dados anônimos anteriores não são importados automaticamente. A interface informa quando uma alteração ainda não foi salva na conta. Cookies de sessão ficam fora desses dados. O ícone e o texto do botão de som refletem o estado ligado/desligado. Movimento reduzido e economia de dados são respeitados.

A abertura fornecida tem 30 segundos, em H.264/AAC, com enquadramento quadrado completo e versões de 720 px para computador e 480 px para celular. Começa sem som, permite pular por botão ou Escape e aparece na primeira visita, conforme a preferência. Falha de carregamento libera a interface. O vídeo é pausado e descarregado ao sair da abertura ou iniciar uma operação. **Rever abertura** e as frases de apresentação ficam somente na ajuda.

A aba de créditos apresenta **Richard Ittou**, com a foto fornecida em preto e branco e ondas azuis animadas, além do [Instagram do criador](https://www.instagram.com/richard.ittou?stkn=c210bHhzdzJwcWg1).

## Login e dados da conta

A aplicação vinculada é `app_3KOHNQeDj1Txbu9tWmOsV6X8bUl`. O Clerk CLI autenticou e vinculou o projeto; a detecção automática não reconheceu FastAPI, então a integração segue os quickstarts oficiais de JavaScript e Python. O navegador carrega `@clerk/clerk-js` 6 e `@clerk/ui` 1 pelo Frontend API desta aplicação, com tradução oficial para português. O servidor usa `clerk-backend-api`, verifica assinatura, validade, emissor, sessão e origem autorizada e determina a conta pelo token, sem aceitar um identificador de usuário no corpo.

Somente a configuração pública de login e o health check são públicos. As operações do estúdio exigem `Authorization: Bearer <token de sessão>`. A aplicação inicia depois da verificação da sessão e da leitura dos dados da conta. Logout ou troca de usuário encerra requisições, mídias e dados ativos. Cookies fornecidos para plataformas externas e URLs temporárias do player não são sincronizados. Os arquivos de mídia baixados continuam no dispositivo, enquanto a conta guarda os links e ajustes.

A configuração inicial usa a instância **de desenvolvimento** escolhida pelo proprietário. A instância de produção do Clerk requer domínio próprio e DNS; chaves de desenvolvimento exibem essa condição no componente de acesso e têm limites específicos do Clerk. Não confunda uma publicação na Vercel com a ativação de uma instância de produção no Clerk.

Variáveis do servidor: `CLERK_SECRET_KEY`, `CLERK_PUBLISHABLE_KEY`, `CLERK_ALLOWED_ORIGINS`, `BLOB_READ_WRITE_TOKEN` e `ONDA_ACCOUNT_BLOB_STORE_ID`. Não coloque a chave secreta em JavaScript. O armazenamento é privado, com documentos de até 32 KB por conta, escrita condicional e erros de sincronização explícitos. No plano gratuito Blob Hobby há cotas de armazenamento e operações; exceder a cota pode deixar as mudanças pendentes, sem declarar uma gravação falsa.

Quando o arquivo está pronto, o botão vermelho de salvar aparece centralizado com fundo desfocado. Ao acioná-lo, a interface volta ao normal. O navegador inicia o download e controla o salvamento; não existe confirmação de conclusão da gravação no dispositivo. Escape fecha o destaque e permite reabri-lo.

## Extração e formatos

Áudio: **MP3, M4A, WAV, FLAC, OGG, OPUS, AAC e AIFF**. Vídeo: **MP4, WebM, MKV e MOV**. O backend utiliza o catálogo explícito dos extratores yt-dlp e links diretos de áudio/vídeo. Autenticação, região, DRM, mudanças da plataforma e bloqueios de IP continuam se aplicando. Playlists e transmissões ao vivo são rejeitadas.

Testes anteriores na Vercel confirmaram extração de TikTok e conversão de fontes diretas. O YouTube público usado como canário foi bloqueado pelo IP da Vercel; APIs públicas gratuitas pesquisadas também não forneceram áudio. Nenhuma API paga foi adicionada. Cookies opcionais não garantem a remoção desse bloqueio.

Limites de processamento:

- Áudio e vídeo: sem rejeição por duração e sem truncamento automático. Fontes finitas sem duração conhecida também são aceitas, até o fim do arquivo.
- Vídeo: origem e saída limitadas a Full HD, em horizontal até 1920×1080 e em vertical até 1080×1920, com saída de até 30 fps.
- Origem: 128 MB agregados entre as faixas. Saída: 100 MB.
- Prazo de trabalho: 240 segundos; espera do cliente: 280 segundos; Function: 300 segundos.
- Duas tarefas concorrentes por instância. Fragmentos HLS/DASH podem ser transferidos com até quatro trabalhadores, compartilhando o mesmo orçamento de bytes e cancelamento.

A resolução nunca amplia a fonte. Fontes acima de Full HD são recusadas; extratores podem escolher uma versão menor quando disponível. Faixas separadas são baixadas pelo transporte validado e unidas localmente. FFmpeg recebe somente arquivos locais. Faixas compatíveis são copiadas para o contêiner de saída sem recodificação quando atendem aos ajustes escolhidos, preservando os pacotes e reduzindo o processamento. Cortes precisos, normalização, mudanças de bitrate, rotação, resolução e codecs incompatíveis usam conversão. Áudio sem perda compatível preserva os samples originais; outras conversões sem perda geram estéreo 44,1 kHz/16 bits. Converter uma fonte comprimida não recupera dados perdidos. O limite por instância não substitui um limitador distribuído em instalações com tráfego elevado. A aceitação sem limite por duração não elimina os limites de tamanho e prazo da hospedagem.

## Ferramentas de mídia

- **Corte por tempo:** início e fim em segundos ou `mm:ss`/`hh:mm:ss`, dentro da duração da mídia, inclusive posições após uma hora.
- **Limpeza de metadados:** ligada por padrão; remove tags pessoais/de autoria, localização, capas/anexos e capítulos. Subtítulos não são incluídos. Campos técnicos gerados pelo contêiner/codificador podem permanecer. Textos e imagens presentes no conteúdo não são alterados.
- **Normalização:** filtro de volume com alvo de −16 LUFS, em uma passagem, quando solicitado. O resultado depende da fonte.
- **Vídeo sem áudio:** remove a faixa sonora; fontes silenciosas também são aceitas.
- **Resolução:** original limitada a Full HD, 1080p, 720p, 480p ou 360p. Em vertical, o lado curto define esses níveis; em horizontal, a altura. Proporção e orientação são preservadas.

MP4, MKV e MOV usam H.264/AAC; WebM usa VP9/Opus. MKV/MOV podem exigir um reprodutor externo ao navegador, mesmo quando o arquivo é válido para salvar.

## Central de Compatibilidade e AutoCura 3.0

A Central mostra as versões reais de yt-dlp, FFmpeg e Deno, extratores e limites. O teste manual permite selecionar qualquer extrator disponível ou mídia direta e inserir um link apropriado. Ele verifica **acesso aos metadados**, sem comprovar download completo, conversão ou autorização de direitos. O provedor escolhido é validado antes de acessar a fonte.

A manutenção consulta evidências reais do GitHub e o relatório da execução. A existência de um workflow, um snapshot de build ou um secret não basta para declarar o AutoCura ativo: é necessária uma execução real bem-sucedida com o relatório correspondente. Consultas têm cache de cinco minutos e não iniciam atualizações a partir do navegador.

O AutoCura usa deploys imutáveis da Vercel, com `scripts/autocura.py` e o modelo `.github/workflows/autocura.yml`:

1. Pesquisa atualizações dos componentes principais, incluindo yt-dlp, FFmpeg e Deno, e resolve as versões compatíveis de todas as dependências indiretas. Cada pacote é verificado e fixado por versão e SHA-256 antes de ser executado. A verificação diária também identifica atualizações compatíveis das dependências indiretas quando os componentes principais permanecem na mesma versão.
2. Constrói uma versão candidata completa sem trocar o domínio ativo.
3. Verifica ferramentas, canários YouTube e conversões de fontes próprias, cobrindo oito formatos de áudio, quatro de vídeo, corte, silêncio, normalização e remoção de metadados.
4. Transfere somente o domínio público `onda-audio.vercel.app` para o candidato aprovado e verifica novamente através desse domínio; uma falha após a ativação restaura o domínio anterior.
5. Persiste relatórios, estado, versões em quarentena e recuperação de uma promoção interrompida no repositório e nos artefatos do workflow. Se o domínio já foi restaurado, conclui a recuperação sem repetir o rollback.

O workflow usa uma política de referência para o YouTube: um bloqueio de IP já existente e idêntico permanece registrado como bloqueado e pode permitir as demais atualizações verificadas. Um novo bloqueio, regressão de extração ou falha dos testes de conversão impede a promoção. O script isolado mantém a política estrita como padrão. A quarentena não confunde automaticamente todo erro de rede com uma versão incompatível.

### Configuração no GitHub

Repositório previsto: **https://github.com/RichardXLR/BraXYTDown**. O projeto desktop existente deve ser preservado. Publique os arquivos deste site em **`onda-web/`** e copie o modelo de workflow para **`.github/workflows/onda-autocura.yml` na raiz do repositório**. O modelo executa os comandos dentro de `onda-web/`; o workflow interno serve como modelo e não é descoberto pelo GitHub nessa subpasta.

Configure os secrets **`CLERK_SECRET_KEY`**, **`CLERK_PUBLISHABLE_KEY`**, **`CLERK_AUTOCURA_MACHINE_SECRET_KEY`** e as variables **`CLERK_AUTOCURA_SOURCE_MACHINE_ID`** / **`CLERK_AUTOCURA_TARGET_MACHINE_ID`**. O AutoCura usa uma identidade Clerk de máquina com tokens curtos, limitada aos canários próprios e metadados dos dois canários YouTube; ela não acessa contas. O workflow sincroniza a configuração do servidor somente após aprovar os testes e também exige a recusa de downloads anônimos. Configure o secret **`VERCEL_TOKEN`**, com acesso ao projeto Vercel, e permita escrita de conteúdo pelo `GITHUB_TOKEN` do workflow. O estado ativo depende de uma execução real bem-sucedida e de seu relatório persistido, além da configuração desse secret. A Central de Compatibilidade consulta essa evidência diretamente no GitHub.

O modelo já inclui os identificadores públicos do projeto e da equipe. Para outro projeto, substitua-os ou defina `VERCEL_PROJECT_ID`, `VERCEL_TEAM_ID` e `VERCEL_ORG_ID` como variables. Se a proteção do deploy exigir, configure também o secret `VERCEL_AUTOMATION_BYPASS_SECRET`. Os canários próprios `/canary.wav` e `/canary.mp4` são CC0 e usados por padrão; URLs públicas alternativas podem ser definidas em `AUTOCURA_AUDIO_CANARY_URL` e `AUTOCURA_VIDEO_CANARY_URL`. Para testar um download YouTube real, configure `AUTOCURA_YOUTUBE_AUDIO_CANARY_URL` com um vídeo curto próprio ou licenciado.

Depois da publicação, execute manualmente **Onda AutoCura 3.0** em Actions com `release` e verifique o resultado e o relatório. A agenda é diária às **05:23 UTC**. Os comandos manuais também permitem testar a versão ativa, fazer rollback e tentar novamente versões em quarentena. Git auto-deploy está desativado no `vercel.json` para impedir publicações que ignorem esses testes.

O build `scripts/package_runtime.py` empacota Deno em `bin/deno` e cria `toolchain.json` com versões e SHA-256 dos executáveis. FFmpeg vem de imageio-ffmpeg. Uma camada de compatibilidade remove somente a tabela informativa DVB SDT/BAT de arquivos MPEG-TS locais para evitar uma falha do FFmpeg estático 7.0.2; os pacotes de áudio/vídeo são preservados. `/canary.ts` é uma fonte sintética CC0 para esse caminho.

## Sessão opcional e segurança

O usuário pode fornecer voluntariamente uma sessão no formato Netscape. O site não lê cookies de outras abas, realiza login no YouTube nem remove limites da conta. Cookies têm limite de 64 KiB/300 entradas e são filtrados por domínios usando a Public Suffix List, enviados somente por HTTPS e mantidos em uma CookieJar exclusiva da requisição. Não são gravados em disco, no histórico, em logs ou em banco. Arquivos diretos não recebem cookies de sessão.

A validação rejeita redes privadas, credenciais em URLs, portas não padrão e esquemas fora de HTTP/HTTPS. O transporte fixa o IP público validado e revalida redirecionamentos. FFmpeg tem protocolos e demuxers limitados. Diretórios temporários são apagados após transmissão, erro ou cancelamento. Arquivos podem exceder 4,5 MB porque as respostas são transmitidas em blocos. A política CSP limita incorporações aos hosts oficiais previstos e bloqueia objetos e captura de câmera/microfone.

## API

- `GET /api/health`: formatos, limites e capacidades de autenticação públicas.
- `GET /api/auth/config`: somente chave publicável e Frontend API.
- `GET /api/account/state`: dados da conta autenticada.
- `PUT /api/account/state`: `{ schema: 1, base_revision, state }`; conflito retorna 409.
- `GET /api/compatibility`: versões, extratores, configuração e canários.
- `GET /api/compatibility/providers`: catálogo completo para seleção de testes.
- `GET /api/maintenance`: estado confirmado da manutenção no GitHub.
- `POST /api/player`: `{ "url": "https://...", "cookies": "opcional, Netscape" }`; retorna uma prévia permitida ou sua indisponibilidade.
- `POST /api/inspect`: `{ "url": "https://...", "media_type": "video", "cookies": "opcional, Netscape" }`.
- `POST /api/compatibility/test`: os mesmos campos de inspeção, com `provider` opcional escolhido no catálogo.
- `POST /api/download`: áudio aceita `{ "url": "https://...", "format": "mp3", "quality": 192, "cookies": "opcional" }`.
- Vídeo aceita `{ "url": "https://...", "media_type": "video", "format": "mp4", "video_resolution": "720", "trim_start": 0, "trim_end": 30, "strip_metadata": true, "mute": false, "normalize_audio": false }`.

`media_type` aceita `audio` (padrão) ou `video`. Ferramentas também funcionam em áudio, exceto `mute` e resolução. Qualidades com perda: 128, 192, 256 ou 320 kbps; formatos sem perda aceitam `source`. Falhas de download usam `{ "error": "mensagem", "code": "categoria" }`. Testes manuais retornam `ok: false` e a categoria da falha quando o acesso não funciona.

## Desenvolvimento, validação e publicação

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
pip install -r requirements-dev.txt
python scripts/package_runtime.py
uvicorn app:app --host 0.0.0.0 --port 8000
```

O frontend usa HTML/CSS/JavaScript e não exige build Node. Verificações:

```sh
pytest -q
node --check public/account.js
node --check public/clerk-portuguese.js
node --check public/app.js
node --check public/ui.js
node --check public/intro.js
node --check public/player.js
```

Os testes usam transporte simulado e FFmpeg real para verificar SSRF, limites, formatos, corte, normalização, metadados, orientação de vídeo, streaming, transferências paralelas, cancelamento e isolamento de cookies. Testes de player cobrem fontes permitidas, incorporações e restrições; testes de manutenção exigem evidência correspondente do GitHub. Testes do AutoCura simulam promoção, rollback, quarentena e recuperação sem alterar produção. Fontes próprias/sintéticas não comprovam reprodução ou extração de cada rede social.

Importe na Vercel como FastAPI ou publique com `vercel deploy --prod` a partir da pasta do site. Não inclua `.venv`, `node_modules`, artefatos ou testes. O fluxo automático descrito acima é a opção para atualizar componentes com testes antes da promoção.

Use somente conteúdo próprio, em domínio público ou com autorização/licença para baixar. Um conteúdo publicamente acessível pode estar protegido por copyright.

## Referência e licenças

Os conceitos de AutoCura e sessão Netscape foram adaptados aos requisitos e à estrutura do ZIP BraXYTDown-main fornecido pelo usuário. O projeto original é desktop Windows/PySide6 e declara licença proprietária. Esta implementação web foi escrita separadamente e preserva o aplicativo e seus avisos no repositório.

As dependências mantêm suas licenças. Consulte `public/third-party-notices.txt` para os avisos e links de licença e código-fonte correspondente dos componentes utilizados, incluindo os binários FFmpeg.
