# Histórico de desempenho

## Implementação

A aba **Desempenho** consulta `/api/downloads/history` e, quando disponível, `/api/jobs/history`. A identidade vem da sessão Clerk verificada, nunca de parâmetros enviados pelo navegador. A interface mostra gráficos das últimas 24 execuções e uma tabela de registros reais. Não inclui demonstrações ou velocidades estimadas apresentadas como medições.

Cada execução aceita pelo pipeline registra data, caminho de processamento, mídia/formato, duração, bytes recebidos da fonte, bytes produzidos, resultado e código técnico seguro. CPU é o delta observado de tempo do processo e subprocessos finalizados. RAM é o maior pico individual reportado por `getrusage` para processo próprio ou filhos, cumulativo desde a inicialização; não é o pico agregado exclusivo de uma tarefa, a memória atual, nem a RAM do telefone. Concorrência pode compartilhar estas observações.

**Vazão média = bytes recebidos / duração total medida.** Inclui processamento e, no caminho inline, entrega HTTP; não é a velocidade instantânea da conexão de origem. Esta permanece no progresso ao vivo quando disponível. Uma execução retomada com entradas já adquiridas pode receber zero bytes da origem.

## Persistência e proteção

O caminho Vercel usa o Blob privado já configurado, em `metrics/v1/<hash-da-conta>.json`, separado dos documentos de preferências. Escritas condicionais com ETag e até três tentativas resolvem concorrência entre instâncias sem sobrescrever outros registros. Identificadores de execução evitam duplicatas. O documento tem até 32 KiB e 120 registros; pode conter menos registros para respeitar o orçamento em bytes. Registros de mais de 30 dias não são exibidos e são descartados na próxima gravação. Não há limpeza diária de contas inativas no Blob.

O worker opcional mantém medições no SQLite persistente, separadas da tarefa que expira. Os dados de sessão e cookies não entram em nenhum registro, log, gráfico ou resposta de histórico. O frontend cancela consultas e limpa o painel ao sair da conta; conteúdos remotos são inseridos como texto.

Medições são gravadas depois de liberar os recursos de mídia. Uma falha de armazenamento não invalida um arquivo já concluído. Interrupção abrupta da Function ou indisponibilidade do Blob pode deixar uma execução sem registro: este caminho não é uma fila durável de telemetria. Falhas de autenticação e rejeições anteriores à admissão não geram registros de processamento. O botão **Atualizar medições** permite consultar novamente após a sincronização.

## Arquivos modificados

- `api/download_history.py`: medidas, validação, Blob privado e API.
- `api/index.py`: hooks de sucesso, falha, cancelamento e finalização HTTP.
- `api/account.py`: timeout configurável do transporte Blob, preservando os valores das preferências.
- `api/jobs.py` e `api/worker.py`: histórico opcional, limpeza, velocidade de retomadas e coordenação entre threads.
- `public/download-history.js`, `download-history.css`, `index.html`, `account.js`: painel, controles de acesso e navegação.
- `public/download-storage.js`: validação de tamanho e estado no fallback em memória.
- `tests/test_download_history.py` e `test_history_storage_browsers.py`: isolamento, concorrência, limites, protocolo, UI e integração dos hooks.
- `.github/workflows/onda-browser-quality.yml`: matriz Chromium/Firefox/WebKit.

O Clerk permanece em desenvolvimento por decisão do usuário. Não houve alteração de chaves, domínio de autenticação, contas ou DNS. Não foram provisionados serviços pagos, worker externo ou armazenamento adicional de arquivos de mídia.

## Publicação validada em 11/10/2026

- Código publicado: `ca1a5f3823b1b37c85d6606ddea563a5427726de`.
- Deploy de produção: `dpl_GnjEiAPyu5X3yfvEz89uj1YDTA19`.
- [Matriz de navegadores aprovada no GitHub](https://github.com/RichardXLR/BraXYTDown/actions/runs/38185404349).
- [Release AutoCura aprovado](https://github.com/RichardXLR/BraXYTDown/actions/runs/38185558621).
- Domínios `ondaittoux.online`, `www.ondaittoux.online` e `onda-audio.vercel.app`: HTTP 200, mesmo deploy, painel presente no HTML e hashes dos quatro arquivos de interface/armazenamento conferidos. Histórico sem autenticação retorna HTTP 401; worker permanece desativado.
- Suíte local inicial: 1.256 testes sem falhas/erros/skips; depois, novos casos e verificação final de histórico/worker aprovados. A matriz de widgets totaliza 102 casos e o fluxo completo HTTP totaliza seis casos (MP4 e MP3 em cada motor), todos aprovados. Estes grupos possuem sobreposição com a suíte inicial: não some os números como testes únicos adicionais.
- Safari em macOS/iPhone e aparelhos Android físicos não foram testados. Veja a matriz manual em `BROWSER-VALIDATION.md`.

Resultados numéricos e evidência de publicação estão em `HISTORICAL-VERIFICATION-2026-10-11.json`. As rotas protegidas foram validadas com identidade de teste local; o teste público de produção validou a proteção anônima, sem acessar os dados de qualquer usuário.
