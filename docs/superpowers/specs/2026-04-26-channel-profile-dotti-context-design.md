# Design Spec — Channel Profile + DOTTI Contextualizado + Correções

**Data:** 2026-04-26  
**Status:** Aprovado pelo usuário — v2 (revisado)

---

## Problema

1. Scripts e prompts gerados sem conhecimento do nicho, subtema ou estilo do canal.
2. Scripts com estrutura repetitiva entre vídeos — YouTube e viewers identificam como conteúdo de IA.
3. DOTTI gera prompts Veo3 sem ancorar no contexto histórico/temporal do roteiro, causando cenas anacrônicas.
4. Timestamps dos prompts Veo3 podem se sobrepor e numeração pode ter gaps/duplicatas, bugando o Veo3 Flow.
5. Geração de descrição ignora o idioma do canal.

---

## Solução — Abordagem C

Perfil estruturado do canal + variação estrutural obrigatória nos roteiros + fase de ancoragem contextual no DOTTI + validações automáticas.

---

## 1. Perfil Estruturado do Canal

### 1.1 Novos campos na tabela `channels`

**Migration — dois passos obrigatórios:**

**Passo 1:** Adicionar as 5 colunas ao `CREATE TABLE IF NOT EXISTS channels` em `init_production_tables()` — necessário para instalações novas (fresh install).

**Passo 2:** Adicionar loop de `ALTER TABLE channels ADD COLUMN` com try/except por coluna — necessário para bancos já existentes (mesmo padrão do `duration_seconds` em `init_db`). Ambos os passos são necessários; omitir o Passo 1 quebra fresh installs, omitir o Passo 2 quebra upgrades.

```python
# Passo 2 — migration para bancos existentes
for col, definition in [
    ("tema_principal",    "TEXT DEFAULT ''"),
    ("subtema",           "TEXT DEFAULT ''"),
    ("tipo_canal",        "TEXT DEFAULT ''"),
    ("instrucoes_roteiro","TEXT DEFAULT ''"),
    ("instrucoes_visuais","TEXT DEFAULT ''"),
]:
    try:
        c.execute(f"ALTER TABLE channels ADD COLUMN {col} {definition}")
    except Exception:
        pass  # coluna já existe
```

| Campo | Exemplo de valor |
|---|---|
| `tema_principal` | "Reconstrução com IA" |
| `subtema` | "Batalhas medievais europeias séc. XIII–XV" |
| `tipo_canal` | "Documentário histórico narrado com IA visual" |
| `instrucoes_roteiro` | "Tom sério e imersivo, linguagem acessível, topo de funil" — **direcional apenas, nunca prescreve estrutura narrativa** |
| `instrucoes_visuais` | "Apenas Europa medieval séc. XIII–XV. Proibido elementos modernos, fantasia ou anacronismos." |

O campo `description` existente permanece como briefing geral do canal.

### 1.2 Funções de banco

Adicionar `update_channel(channel_id, **fields)` em `database.py` — atualiza apenas os campos passados via kwargs, usando `UPDATE channels SET k=? WHERE id=?` em loop. Necessário para salvar perfil gerado pela IA e para edição manual.

### 1.3 Endpoint de edição

**Novo endpoint:** `PUT /api/channels/<id>` que recebe JSON com qualquer subconjunto dos campos do canal e chama `update_channel`. Usado tanto pela edição manual quanto pela confirmação do perfil gerado.

### 1.4 Geração automática do perfil

**Endpoint:** `POST /api/channels/<id>/generate-profile`

Claude analisa as produções do canal. Input capeado em no máximo 10 produções × 500 chars de título/script cada (evita truncation silenciosa). Retorna sugestão dos 5 campos como JSON.

**Fluxo:**
1. Frontend mostra preview dos campos sugeridos (editáveis pelo usuário)
2. Usuário confirma ou edita
3. `PUT /api/channels/<id>` salva

**Nunca salva automaticamente** — sempre passa pela revisão do usuário.

---

## 2. Variação Estrutural Obrigatória nos Roteiros

### 2.1 Problema

Scripts gerados com o mesmo padrão de abertura e estrutura entre vídeos são identificados como conteúdo de IA pelo YouTube e perdem distribuição orgânica.

### 2.2 Solução

O system prompt de geração de roteiro inclui instrução explícita de variação estrutural. A lista de estilos de abertura é expandida e o Claude recebe instrução de escolher deliberadamente uma abertura diferente a cada geração:

```
VARIAÇÃO ESTRUTURAL OBRIGATÓRIA:
Cada roteiro deve começar de forma estruturalmente diferente dos roteiros padrão de IA.
Escolha uma das seguintes abordagens de abertura (varie entre vídeos, nunca repita a mesma sequência):
- Começar com uma pergunta retórica provocativa
- Começar in media res (no meio de uma cena ou evento)
- Começar com um dado ou estatística surpreendente
- Começar com uma citação histórica ou frase atribuída
- Começar com uma descrição sensorial de lugar/tempo
- Começar com uma contradição ou paradoxo histórico
- Começar com a conclusão e trabalhar o caminho até ela

A estrutura interna também deve variar: nem sempre cronológica, nem sempre 3 atos.
O objetivo é que cada vídeo pareça escrito por um narrador diferente com perspectiva própria.
```

Isso complementa os `instrucoes_roteiro` do canal (que direcionam tom e registro) sem engessar a estrutura.

---

## 3. Contexto do Canal no Roteiro e na Descrição

### 3.1 Injeção no script

Quando os campos de perfil estão preenchidos, o system prompt recebe bloco adicional:

```
PERFIL DO CANAL:
Tema principal: {tema_principal}
Subtema: {subtema}
Tipo: {tipo_canal}
Direcionamento narrativo: {instrucoes_roteiro}
```

**Regra de omissão:** cada linha é omitida individualmente se o campo estiver vazio. Se todos os campos estiverem vazios, o bloco inteiro é omitido. Nunca envia campos em branco ao modelo.

### 3.2 Fix de idioma na descrição

Bug: `_auto_trigger_description` usava um dict inline que não continha `"en"` e defaultava para Italian.

Fix: substituir o dict inline pelo `_LANG_MAP` existente (linhas 106-110 em `app.py`) como única fonte de verdade para mapeamento de idioma. Aplicar o mesmo padrão já usado na geração de roteiro: `lang_name = _LANG_MAP.get(lang_code, "portuguese")`.

**Nota:** bug confirmado no código atual — `_auto_trigger_description` tem dict inline sem a chave `"en"` e defaulta para `"Italian"`. Este fix é obrigatório nesta implementação.

---

## 4. DOTTI com Ancoragem Contextual (Phase 0)

### 4.1 Fase 0 — Extração de âncora

Chamada separada ao Claude antes de gerar os prompts. Lê o roteiro completo e retorna JSON:

```json
{
  "periodo": "Bizâncio, séc. X, reinado de Constantino VII",
  "localizacao": "Constantinopla — Palácio Imperial, Hipódromo",
  "restricoes": [
    "apenas arquitetura e vestuário bizantino do séc. X",
    "sem anacronismos ou elementos de outras épocas",
    "sem personagens identificáveis"
  ]
}
```

**`elementos_visuais_chave` removido** — não entra na âncora para não engessar os prompts individuais. As `restricoes` são o que **não pode** aparecer, não o que deve aparecer.

**Fallback:** se o JSON retornado for inválido ou incompleto, a Fase 0 é silenciosamente ignorada e os prompts são gerados sem âncora (comportamento atual). Log de aviso é emitido. Não bloqueia a geração.

### 4.2 Fase 1 — Geração dos prompts

O `user_msg` existente é **estendido** (não substituído) com cabeçalho antes do roteiro:

```
ÂNCORA CONTEXTUAL DO EPISÓDIO (extraída do roteiro — respeitar rigorosamente):
Período: {periodo}
Localização: {localizacao}
Restrições absolutas de cena: {restricoes_formatadas}

PERFIL VISUAL DO CANAL:
{instrucoes_visuais}  ← omitido se vazio

ROTEIRO COMPLETO:
{script_text}
[... resto do user_msg existente ...]
```

### 4.3 Atualização de `dotti_agent.txt`

Adicionar ao início do system prompt o seguinte parágrafo:

```
IMPORTANTE: O contexto enviado pelo usuário inclui uma ÂNCORA CONTEXTUAL no topo.
Todos os prompts gerados DEVEM respeitar o período histórico, localização e restrições
definidas na âncora. Qualquer elemento visual que viole as restrições absolutas é inaceitável,
independentemente do que pareça esteticamente interessante. Fidelidade histórica ao período
definido é não-negociável.
```

---

## 5. Validação de Timestamps e Numeração dos Prompts

### 5.1 O que validar

Após a geração do texto completo pelo DOTTI, uma função `_validate_and_fix_prompts(text)` faz:

**A) Numeração sequencial:**
- Extrai todos os números de prompt (padrão: `PROMPT 001`, `PROMPT 035`, etc.)
- Verifica se são sequenciais sem gaps e sem duplicatas
- Se houver problema, renumera todos sequencialmente a partir de 001 com zero-padding correto

**B) Timestamps:**
- Extrai todos os timestamps no formato `MM:SS - MM:SS` ou `HH:MM:SS - HH:MM:SS`
- Converte para segundos para comparação
- Detecta sobreposições: onde o `start` de um prompt é menor que o `end` do prompt anterior
- Corrige sobreposições ajustando o `start` do prompt seguinte para ser igual ao `end` do anterior
- Gaps entre prompts são mantidos (são válidos no Veo3 Flow)
- Cada timestamp corrigido é reconvertido para o formato original (MM:SS)

**Extração de timestamp:** usar regex ancorada no prefixo `PROMPT NNN [] |` para evitar falsos positivos com outros `:` no texto dos prompts. Padrão: `r'PROMPT\s+(\d+)\s*\[.*?\]\s*\|\s*(\d{1,2}:\d{2})\s*-\s*(\d{1,2}:\d{2})'`.

**C) Consistência número × timestamp:**
- Após corrigir numeração e timestamps separadamente, verificar que o prompt N tem timestamp coerente com o prompt N-1 (start ≥ end anterior)
- Log de todas as correções feitas para auditoria

### 5.2 Aplicação

A função é chamada ao final de `_bg_prompts` antes de salvar o `result_text` no banco. O texto corrigido é o que é salvo.

---

## 6. Arquivos Afetados

| Arquivo | Mudança |
|---|---|
| `database.py` | Migration dos 5 novos campos via ALTER TABLE; nova função `update_channel()` |
| `app.py` | `PUT /api/channels/<id>`; `POST /api/channels/<id>/generate-profile`; injeção de perfil no script; variação estrutural no system prompt de script; DOTTI Phase 0; `_validate_and_fix_prompts()`; fix de idioma na descrição via `_LANG_MAP` |
| `prompts/dotti_agent.txt` | Parágrafo de âncora contextual no início do system prompt |
| `templates/channel_detail.html` | Seção de perfil do canal na edição; botão "Gerar perfil com IA"; preview de sugestão |

---

## 7. Fora do Escopo

- Sugestão automática de atualização de perfil a cada 5 novos vídeos
- Campos de referência de canais EN no perfil
