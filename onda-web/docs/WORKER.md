# Worker persistente do Onda

A aplicação mantém o caminho de streaming da Vercel e adiciona um worker opcional para tarefas longas e arquivos grandes. Não cria nenhum serviço pago. Um processo sempre ativo e um volume persistente são necessários para a fila sobreviver a reinicializações. O disco efêmero de uma Function da Vercel não atende a esse requisito.

## Inicialização

Instale `requirements.txt` com Python 3.12 ou mais recente. Defina no servidor do worker, pelo gerenciador de segredos:

- `ONDA_WORKER_MODE=true`
- `ONDA_JOB_DIR=/data/onda` (volume persistente privado, fora de `/tmp`)
- `ONDA_JOB_SECRET`: chave aleatória de **32 bytes**, codificada em base64 URL-safe. Preserve esta chave entre reinicializações; não a publique nem a envie no chat.
- `ONDA_WORKER_PUBLIC_URL=https://worker.seu-dominio` (endereço HTTPS público da API)
- As mesmas variáveis Clerk já usadas no site, incluindo a lista de origens autorizadas. A origem do Onda deve constar nessa lista.

Execute dois processos supervisionados no mesmo servidor/volume:

```sh
python scripts/run_worker.py
uvicorn app:app --host 0.0.0.0 --port 8000 --no-access-log
```

Coloque a API atrás de um proxy HTTPS. **Desative ou remova os parâmetros de consulta dos logs** do proxy: os links temporários de entrega são credenciais de acesso ao arquivo. Configure o proxy para permitir streaming e Range sem buffering integral. Não exponha o volume como diretório estático. O processo API e o worker devem usar a mesma conta do sistema, com arquivos privados (diretórios 0700, dados 0600, saída concluída 0400).

Na Vercel, configure somente `ONDA_WORKER_URL=https://worker.seu-dominio`. O site consulta a saúde do worker e só seleciona a fila se ela estiver ativa. O token Clerk do usuário é encaminhado para uma origem fixa, sem redirecionamentos; a API do worker também verifica esse token. O arquivo pronto é servido diretamente pelo worker por link assinado de até 20 minutos, com suporte a Range e ETag SHA-256.

## Capacidade e operação

O perfil worker permite até 4 GiB de origem e de saída, 12 GiB de transferência acumulada incluindo novas tentativas, 8 GiB de área de trabalho e 30 minutos de processamento por tentativa. A execução verifica espaço real e mantém uma reserva mínima de 64 MiB. Esses valores são limites operacionais configuráveis, **não uma garantia de que qualquer origem conclua a tarefa**.

Variáveis opcionais: `ONDA_MAX_SOURCE_BYTES`, `ONDA_MAX_OUTPUT_BYTES`, `ONDA_MAX_TRANSFER_BYTES`, `ONDA_MAX_WORKSPACE_BYTES`, `ONDA_RESERVE_BYTES`, `ONDA_PROCESS_SECONDS`, `ONDA_TOTAL_SECONDS`, `ONDA_FRAGMENT_WORKERS`. Use valores compatíveis com o disco, CPU e rede disponíveis. Comece com **um processo worker**; o banco admite até dois trabalhos simultâneos e somente um por conta. Arquivos preparados e tarefas expiram em 24 horas; a limpeza respeita tarefas ativas. Até quatro tarefas pendentes por conta e 1.000 pendentes no serviço (`ONDA_MAX_PENDING_JOBS`, ajustável entre 2 e 10.000). A admissão verifica a reserva de disco antes de gravar a solicitação. Uma tarefa expirada durante o processamento encerra de forma controlada. Prioridades de 0 a 2, com envelhecimento da fila.

A fila usa SQLite WAL e transações. Solicitações e checkpoints sensíveis são criptografados com AES-GCM, vinculados à tarefa; identificadores de conta são derivados com HMAC. Idempotência é por conta e chave de solicitação. Leases com fencing impedem um worker antigo de publicar um resultado; bloqueio do diretório impede dois processos de escrever nos mesmos arquivos. Uma tarefa cancelada não pode voltar ao estado pronto.

Checkpoints de fontes adquiridas incluem SHA-256 e são reutilizados após reinicialização se a integridade for válida. Transferências HTTP diretas com ETag forte ou Last-Modified usam Range/If-Range; checkpoints persistentes são gravados a cada 8 MiB. O trecho sem checkpoint é descartado no reinício. A origem precisa manter a mesma representação. Fontes HLS/DASH já concluídas podem ser reutilizadas; fragmentos incompletos de todas as plataformas não têm retomada persistente garantida.

Falhas temporárias têm tentativas limitadas. `Retry-After` nunca é encurtado. Sessões expiradas, DRM e ausência de autorização exigem ação do usuário. O worker não renova cookies de redes sociais por mecanismos não oficiais.

## Expansão

SQLite exige API e workers no mesmo servidor e volume. Para múltiplos servidores, substitua esse armazenamento por uma fila e banco compartilhados, mantendo idempotência, fencing e isolamento; entregue arquivos por armazenamento de objetos. Redis não é necessário para a instalação de um servidor. Faça backup criptografado do banco e proteja a chave separadamente.

## Histórico de execução

O worker registra medições numéricas por conta em uma tabela separada da fila: tempo, bytes recebidos, tamanho produzido, tentativa, resultado, código de erro e observações de CPU/RAM do processo. A exclusão da tarefa após 24 horas não apaga o histórico recente. O histórico tem no máximo 120 registros por conta e uma janela de 30 dias; a limpeza periódica também remove as medições antigas. Não contém URL, cookies, payload ou checkpoints. A rota protegida `/api/jobs/history` só consulta o proprietário autenticado.

Esta alteração não ativa o worker por conta própria. O usuário optou por manter a hospedagem atual sem servidor externo; SQLite, gateway e worker continuam opcionais. Nenhuma fila compartilhada ou dependência S3/PostgreSQL foi adicionada à versão publicada.
