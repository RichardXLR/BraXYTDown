# Fila persistente e pacotes ZIP

Onda usa Vercel Queues e o Blob privado já conectado ao projeto. A requisição do usuário grava o pedido e retorna HTTP 202; funções privadas da fila processam cada arquivo e, por fim, montam o ZIP. Nenhuma aba aberta ou máquina do usuário é necessária para executar a fila.

As funções são ativadas sob demanda. Esta implementação oferece persistência de mensagens, etapas concluídas e resultados; não cria uma máquina virtual ligada permanentemente.

## Componentes

- `api/batches.py`: admissão, idempotência, isolamento por conta Clerk, consulta, cancelamento, repetição dos pendentes e entrega parcial autenticada.
- `api/batch_store.py`: documentos condicionais por ETag, pedidos cifrados com AES-GCM e transferência privada em blocos.
- `api/batch_worker.py`: uma etapa por invocação, lease com token, reaproveitamento dos arquivos concluídos, recuperação limitada, ZIP sem recomprimir mídia e verificação SHA-256/CRC.
- `api/batch_subscriber.py` e `api/batch_cleanup.py`: consumidores privados com tentativas limitadas e limpeza retardada.
- `public/batch.js`: restaurar a fila da conta, acompanhar dados reais e receber ZIP por intervalos de até 2 MiB, com retomada do intervalo interrompido e armazenamento temporário em disco quando disponível.
- `api/batch_probe.py` e AutoCura: prova de execução da fila e canário de dois arquivos próprios com ZIP validado antes da promoção.

## Segurança e recuperação

Os payloads das mensagens contêm apenas hashes de proprietário e identificadores de tarefa. URLs, cookies e senhas de vídeo ficam cifrados em objetos privados; a chave é derivada com separação de domínio do segredo Clerk configurado no servidor. Trocar esse segredo torna os pedidos anteriores ilegíveis; é preciso esvaziar a fila antes de rotacioná-lo ou migrar as chaves.

As rotas públicas continuam exigindo sessão Clerk. A identidade de manutenção pode usar somente os canários autorizados; não pode acessar a fila de usuários. Caminhos e URLs de armazenamento são derivados pelo servidor e não são expostos ao cliente. A extração reaproveita a validação de origem/SSRF, restrições de sessão, limites de disco e FFmpeg existentes.

Um lease de 360 segundos impede dois consumidores de publicar a mesma etapa simultaneamente. Mensagens duplicadas usam idempotência e gravações condicionais. Depois de uma interrupção de função, o consumidor pode reassumir o lease expirado. Arquivos já guardados permanecem disponíveis; a etapa interrompida é refeita. Transferências da fonte continuam usando a recuperação do motor existente dentro da invocação. Não há promessa de retomada parcial de um arquivo de origem entre funções reiniciadas.

Falhas temporárias têm até três tentativas de etapa, além das tentativas limitadas do consumidor. Login obrigatório, sessões rejeitadas e outras restrições permanentes são exibidos sem tentativas infinitas. O ZIP pode conter os arquivos que concluíram; os pendentes podem ser repetidos em outro pacote com cookies e ajustes atuais.

## Limites reais desta implantação gratuita

- Plano Hobby confirmado na conta do projeto; Functions continuam com máximo de 300 segundos. O processamento de cada etapa usa orçamento de 230 segundos.
- Até 12 links por pacote e quatro pacotes ativos por conta; uma etapa de mídia por vez no consumidor compartilhado.
- ZIP final de até 350 MiB (incluindo cabeçalhos), sujeito ao espaço temporário e aos limites já existentes do motor. Não constitui suporte validado a pacotes de 1 GiB.
- Resultados disponíveis por seis horas a partir da criação. Uma mensagem retardada remove os pedidos, mídia intermediária, ZIP e a reserva de espaço. Após cancelamento, a limpeza aguarda até dez minutos para não competir com uma transferência já em execução; tarefas ainda não iniciadas podem ser limpas antes.
- Reserva global de até 768 MiB, contando mídia intermediária e ZIP durante o empacotamento. O Blob Hobby inclui 1 GB médio/mês, 2.000 operações avançadas, 10.000 simples e 10 GB de transferência, compartilhados com o restante do aplicativo. Estes valores foram consultados na documentação vigente durante a implementação; não são armazenamento ilimitado.
- Admissão reserva conservadoramente `30 + 24 × número de links` operações avançadas, até 1.200 reservas por mês, deixando margem para contas e manutenção. A reserva não consulta o consumo faturado real: as cotas do provedor também podem interromper operações. Receber repetidamente um ZIP consome transferência novamente.
- `ONDA_BATCH_ENABLED=true` ativa o recurso após a configuração revisada em CI. Nenhum serviço pago ou servidor externo permanente foi contratado. Caso a franquia gratuita se esgote, novas operações são recusadas com uma mensagem de capacidade; a implementação não altera o plano.

## Verificação

Testes automatizados verificam processamento após fechar a requisição, múltiplas invocações, FFmpeg com mídia própria, ZIP e integridade, isolamento entre usuários, cifragem e adulteração, idempotência, cancelamento, lease expirado, recuperação temporária, falhas de autorização e repetição exclusiva dos pendentes.

O teste de navegador fecha a página após enviar a lista, executa o processamento no servidor, volta à conta e salva o ZIP. A mesma prova roda em Chromium, Firefox e WebKit. Estes são navegadores de laboratório; não equivalem a Safari em iPhone ou testes em celulares físicos.

O AutoCura exige uma prova real na Vercel: mensagem aceita, consumidor privado executado, dois MP3 gerados e ZIP com CRC válido. Publicação só é promovida quando esses canários e os controles existentes passam.

Para crescer além dessas cotas, a arquitetura permite elevar a capacidade do armazenamento/filas e mover etapas de mídia para um worker com disco e tempo maiores. Essa expansão fica desativada nesta versão.
