"""
Agente de Monitoramento de Vagas — Diário Oficial do Município de BH (DOM-BH)
=============================================================================
Requisitos:
    pip install requests beautifulsoup4 pdfplumber anthropic schedule

Configuração:
    Edite a seção CONFIG abaixo com sua chave da API Anthropic e
    as credenciais de notificação (e-mail ou Telegram).

Uso:
    python agente_dom_bh.py                  # roda uma vez imediatamente
    python agente_dom_bh.py --agendar        # fica rodando todo dia útil às 9h
    python agente_dom_bh.py --data 2026-05-12 # processa uma edição específica
"""

import os
import re
import sys
import time
import logging
import smtplib
import argparse
import tempfile
import requests
import schedule
import pdfplumber

from datetime import date, datetime, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from pathlib import Path
from bs4 import BeautifulSoup

import anthropic

# =============================================================================
# CONFIG — edite aqui
# =============================================================================

CONFIG = {
    # Chave da API Anthropic (ou defina a variável de ambiente ANTHROPIC_API_KEY)
    "anthropic_api_key": os.getenv("ANTHROPIC_API_KEY", "SUA_CHAVE_AQUI"),

    # ── Notificação por E-mail ────────────────────────────────────────────────
    "email": {
        "ativo": False,                          # mude para True para habilitar
        "smtp_host": "smtp.gmail.com",
        "smtp_port": 465,
        "usuario": "seu_email@gmail.com",
        "senha": "sua_senha_de_app",             # senha de app do Gmail
        "destinatarios": ["voce@email.com"],
    },

    # ── Notificação por Telegram ──────────────────────────────────────────────
    "telegram": {
        "ativo": False,                          # mude para True para habilitar
        "bot_token": "SEU_TOKEN_AQUI",
        "chat_id": "SEU_CHAT_ID_AQUI",
    },

    # ── Comportamento ─────────────────────────────────────────────────────────
    "horario_execucao": "09:00",                 # hora da verificação diária
    "salvar_pdfs": True,                         # salva o PDF baixado em disco
    "pasta_pdfs": "./pdfs_dom_bh",               # pasta para salvar PDFs
    "max_tokens_pdf": 40000,                     # limite de chars enviados ao LLM
}

# Palavras-chave que indicam uma publicação de vagas/seleção
KEYWORDS_VAGAS = [
    "concurso público", "processo seletivo", "edital", "vaga",
    "contratação temporária", "seleção simplificada", "inscrição",
    "provimento", "cadastro reserva", "estágio", "trainee",
    "processo de seleção", "chamamento", "credenciamento",
]

# =============================================================================
# Logging
# =============================================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("dom_bh")

# =============================================================================
# 1. Obtenção do PDF
# =============================================================================

class DOMDownloader:
    """
    Tenta baixar o PDF do DOM-BH por três estratégias, em ordem de preferência:
      1. API interna do dom-web.pbh.gov.br  (endpoint JSON)
      2. Página de contingência da PBH       (prefeitura.pbh.gov.br/dom-web)
      3. Jusbrasil                           (fallback, scraping HTML)
    """

    HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )
    }

    # ── Estratégia 1: API interna do portal DOM ───────────────────────────────
    def _tentar_api_dom(self, data: date) -> bytes | None:
        """
        O portal dom-web.pbh.gov.br usa uma API REST interna.
        Tentamos descobrir a edição do dia e baixar seu PDF.
        """
        try:
            # Busca a edição mais recente via endpoint de listagem
            url_api = "https://dom-web.pbh.gov.br/api/edicoes"
            params = {"data": data.strftime("%Y-%m-%d")}
            resp = requests.get(url_api, params=params, headers=self.HEADERS, timeout=15)
            if resp.status_code == 200:
                dados = resp.json()
                # Estrutura esperada: [{"id": 123, "dataPublicacao": "...", ...}]
                if dados:
                    edicao_id = dados[0].get("id") or dados[0].get("edicaoId")
                    pdf_url = f"https://dom-web.pbh.gov.br/api/edicoes/{edicao_id}/pdf"
                    r = requests.get(pdf_url, headers=self.HEADERS, timeout=30)
                    if r.status_code == 200 and r.headers.get("content-type", "").startswith("application/pdf"):
                        log.info(f"[Estratégia 1] PDF obtido via API DOM (edição {edicao_id})")
                        return r.content
        except Exception as e:
            log.debug(f"[Estratégia 1] Falhou: {e}")
        return None

    # ── Estratégia 2: página de contingência da PBH ───────────────────────────
    def _tentar_pbh_contingencia(self, data: date) -> bytes | None:
        """
        Durante manutenções, a PBH publica o PDF em prefeitura.pbh.gov.br/dom-web
        com um link direto para o arquivo.
        """
        try:
            url = "https://prefeitura.pbh.gov.br/dom-web"
            resp = requests.get(url, headers=self.HEADERS, timeout=15)
            soup = BeautifulSoup(resp.text, "html.parser")
            for a in soup.find_all("a", href=True):
                href = a["href"]
                if href.lower().endswith(".pdf"):
                    pdf_url = href if href.startswith("http") else f"https://prefeitura.pbh.gov.br{href}"
                    r = requests.get(pdf_url, headers=self.HEADERS, timeout=30)
                    if r.status_code == 200:
                        log.info(f"[Estratégia 2] PDF obtido via página de contingência PBH")
                        return r.content
        except Exception as e:
            log.debug(f"[Estratégia 2] Falhou: {e}")
        return None

    # ── Estratégia 3: Jusbrasil (fallback) ───────────────────────────────────
    def _tentar_jusbrasil(self, data: date) -> bytes | None:
        """
        O Jusbrasil indexa o DOM-BH. Tentamos baixar o PDF pelo link de download.
        Nota: o Jusbrasil pode bloquear scrapers; use com moderação.
        """
        try:
            data_str = data.strftime("%d-%m-%Y")
            url = f"https://www.jusbrasil.com.br/diarios/DOM-BH/{data_str}"
            resp = requests.get(url, headers=self.HEADERS, timeout=15)
            soup = BeautifulSoup(resp.text, "html.parser")
            for a in soup.find_all("a", href=True):
                if "download" in a.get("class", []) or "pdf" in a["href"].lower():
                    pdf_url = a["href"]
                    r = requests.get(pdf_url, headers=self.HEADERS, timeout=30)
                    if r.status_code == 200 and len(r.content) > 5000:
                        log.info(f"[Estratégia 3] PDF obtido via Jusbrasil")
                        return r.content
        except Exception as e:
            log.debug(f"[Estratégia 3] Falhou: {e}")
        return None

    def baixar(self, data: date) -> bytes | None:
        """Tenta as três estratégias em sequência."""
        log.info(f"Baixando DOM-BH para {data.strftime('%d/%m/%Y')}...")
        for estrategia in [self._tentar_api_dom, self._tentar_pbh_contingencia, self._tentar_jusbrasil]:
            conteudo = estrategia(data)
            if conteudo:
                return conteudo
        log.error("Nenhuma estratégia conseguiu baixar o PDF do DOM-BH.")
        return None


# =============================================================================
# 2. Extração de texto do PDF
# =============================================================================

def extrair_texto_pdf(pdf_bytes: bytes, max_chars: int = 40000) -> str:
    """
    Extrai texto do PDF usando pdfplumber.
    Retorna até max_chars caracteres para não exceder o contexto do LLM.
    """
    texto_total = []
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp.write(pdf_bytes)
        tmp_path = tmp.name

    try:
        with pdfplumber.open(tmp_path) as pdf:
            log.info(f"PDF com {len(pdf.pages)} página(s). Extraindo texto...")
            for i, pagina in enumerate(pdf.pages, 1):
                t = pagina.extract_text()
                if t:
                    texto_total.append(f"\n--- Página {i} ---\n{t}")
                # Para de extrair se já atingiu o limite
                texto_unido = "\n".join(texto_total)
                if len(texto_unido) >= max_chars:
                    log.info(f"Limite de {max_chars} chars atingido na página {i}.")
                    break
    finally:
        Path(tmp_path).unlink(missing_ok=True)

    texto_final = "\n".join(texto_total)[:max_chars]
    log.info(f"Texto extraído: {len(texto_final)} caracteres.")
    return texto_final


def filtrar_secoes_relevantes(texto: str) -> str:
    """
    Pré-filtro: retorna apenas parágrafos que contêm palavras-chave de vagas.
    Reduz tokens enviados ao LLM quando o DOM é muito extenso.
    """
    paragrafos = texto.split("\n")
    relevantes = []
    for para in paragrafos:
        para_lower = para.lower()
        if any(kw in para_lower for kw in KEYWORDS_VAGAS):
            relevantes.append(para)

    if not relevantes:
        return ""

    return "\n".join(relevantes)


# =============================================================================
# 3. Análise com Claude
# =============================================================================

def analisar_com_claude(texto: str, data: date, api_key: str) -> str:
    """
    Envia o texto do DOM ao Claude e pede extração estruturada de vagas.
    """
    client = anthropic.Anthropic(api_key=api_key)
    data_fmt = data.strftime("%d/%m/%Y")

    system = (
        "Você é um especialista em análise de Diários Oficiais brasileiros. "
        "Sua tarefa é identificar e resumir publicações relacionadas a vagas de emprego, "
        "concursos públicos e processos seletivos. Seja objetivo e preciso."
    )

    user = f"""Analise o texto abaixo, extraído do Diário Oficial do Município de Belo Horizonte (DOM-BH) do dia {data_fmt}.

Identifique SOMENTE publicações relacionadas a:
- Abertura de concursos públicos
- Processos seletivos simplificados
- Contratações temporárias
- Estágios e trainees
- Editais de seleção ou credenciamento de pessoal

Para CADA item encontrado, apresente no formato:

📌 **[ÓRGÃO]**
- Cargo(s): ...
- Nº de vagas: ...
- Salário/remuneração: ...
- Prazo de inscrição: ...
- Como se inscrever: ...
- Resumo: (2-3 linhas descrevendo o processo)

---

Se não houver NENHUMA publicação de vagas, responda apenas:
"✅ Sem novas vagas publicadas no DOM-BH de {data_fmt}."

Texto do DOM-BH:
{texto}
"""

    log.info("Consultando Claude para análise do texto...")
    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=2000,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    return response.content[0].text


# =============================================================================
# 4. Notificações
# =============================================================================

def notificar_email(resultado: str, data: date, cfg: dict):
    """Envia o resultado por e-mail."""
    if not cfg.get("ativo"):
        return
    try:
        data_fmt = data.strftime("%d/%m/%Y")
        msg = MIMEMultipart("alternative")
        msg["Subject"] = f"🔔 DOM-BH {data_fmt} — Vagas Publicadas"
        msg["From"] = cfg["usuario"]
        msg["To"] = ", ".join(cfg["destinatarios"])

        corpo_texto = f"DOM-BH — {data_fmt}\n\n{resultado}"
        corpo_html = f"""
        <html><body style="font-family: Arial, sans-serif; max-width: 700px; margin: auto;">
          <h2 style="color: #1a56a0;">📋 DOM-BH — {data_fmt}</h2>
          <pre style="white-space: pre-wrap; background: #f4f4f4; padding: 16px; border-radius: 8px;">
{resultado}
          </pre>
          <p style="color: #888; font-size: 12px;">
            Gerado automaticamente pelo Agente DOM-BH
          </p>
        </body></html>
        """
        msg.attach(MIMEText(corpo_texto, "plain", "utf-8"))
        msg.attach(MIMEText(corpo_html, "html", "utf-8"))

        with smtplib.SMTP_SSL(cfg["smtp_host"], cfg["smtp_port"]) as servidor:
            servidor.login(cfg["usuario"], cfg["senha"])
            servidor.sendmail(cfg["usuario"], cfg["destinatarios"], msg.as_bytes())
        log.info(f"E-mail enviado para: {cfg['destinatarios']}")
    except Exception as e:
        log.error(f"Falha ao enviar e-mail: {e}")


def notificar_telegram(resultado: str, data: date, cfg: dict):
    """Envia o resultado via bot do Telegram."""
    if not cfg.get("ativo"):
        return
    try:
        data_fmt = data.strftime("%d/%m/%Y")
        texto = f"*📋 DOM-BH — {data_fmt}*\n\n{resultado}"
        # Telegram tem limite de 4096 chars por mensagem
        for chunk in [texto[i:i+4000] for i in range(0, len(texto), 4000)]:
            url = f"https://api.telegram.org/bot{cfg['bot_token']}/sendMessage"
            payload = {"chat_id": cfg["chat_id"], "text": chunk, "parse_mode": "Markdown"}
            requests.post(url, json=payload, timeout=10)
        log.info("Mensagem enviada ao Telegram.")
    except Exception as e:
        log.error(f"Falha ao enviar Telegram: {e}")


# =============================================================================
# 5. Pipeline principal
# =============================================================================

def executar(data: date | None = None):
    """Executa o pipeline completo para uma data."""
    if data is None:
        data = date.today()

    # Pula fins de semana (DOM-BH não é publicado sábado/domingo)
    if data.weekday() >= 5:
        log.info(f"{data.strftime('%d/%m/%Y')} é fim de semana. Pulando.")
        return

    log.info(f"=== Iniciando verificação DOM-BH para {data.strftime('%d/%m/%Y')} ===")

    # 1. Baixar PDF
    downloader = DOMDownloader()
    pdf_bytes = downloader.baixar(data)
    if not pdf_bytes:
        msg = f"⚠️ Não foi possível baixar o DOM-BH de {data.strftime('%d/%m/%Y')}. Tente manualmente em dom-web.pbh.gov.br"
        log.warning(msg)
        notificar_email(msg, data, CONFIG["email"])
        notificar_telegram(msg, data, CONFIG["telegram"])
        return

    # Salvar PDF em disco (opcional)
    if CONFIG["salvar_pdfs"]:
        pasta = Path(CONFIG["pasta_pdfs"])
        pasta.mkdir(parents=True, exist_ok=True)
        nome_arquivo = pasta / f"dom_bh_{data.strftime('%Y-%m-%d')}.pdf"
        nome_arquivo.write_bytes(pdf_bytes)
        log.info(f"PDF salvo em: {nome_arquivo}")

    # 2. Extrair texto
    texto = extrair_texto_pdf(pdf_bytes, max_chars=CONFIG["max_tokens_pdf"])
    if not texto.strip():
        log.warning("Texto extraído vazio. O PDF pode ser escaneado (imagem).")
        # Fallback: envia aviso
        texto = "(PDF sem texto extraível — possível imagem escaneada)"

    # 3. Pré-filtrar seções relevantes
    texto_filtrado = filtrar_secoes_relevantes(texto)
    if not texto_filtrado:
        log.info("Nenhuma palavra-chave de vagas encontrada no texto pré-filtrado.")
        resultado = f"✅ Sem novas vagas publicadas no DOM-BH de {data.strftime('%d/%m/%Y')}."
    else:
        # 4. Análise com Claude
        resultado = analisar_com_claude(texto_filtrado, data, CONFIG["anthropic_api_key"])

    log.info(f"\n{'='*60}\n{resultado}\n{'='*60}")

    # 5. Notificar
    notificar_email(resultado, data, CONFIG["email"])
    notificar_telegram(resultado, data, CONFIG["telegram"])

    return resultado


# =============================================================================
# 6. Agendador
# =============================================================================

def iniciar_agendador():
    horario = CONFIG["horario_execucao"]
    log.info(f"Agendador iniciado. Verificação diária às {horario} (dias úteis).")

    schedule.every().monday.at(horario).do(executar)
    schedule.every().tuesday.at(horario).do(executar)
    schedule.every().wednesday.at(horario).do(executar)
    schedule.every().thursday.at(horario).do(executar)
    schedule.every().friday.at(horario).do(executar)

    while True:
        schedule.run_pending()
        time.sleep(30)


# =============================================================================
# 7. Entrada principal
# =============================================================================

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Agente de monitoramento de vagas no DOM-BH"
    )
    parser.add_argument(
        "--agendar",
        action="store_true",
        help="Mantém o processo rodando e executa diariamente no horário configurado",
    )
    parser.add_argument(
        "--data",
        type=str,
        default=None,
        help="Data específica no formato YYYY-MM-DD (padrão: hoje)",
    )
    args = parser.parse_args()

    if args.data:
        try:
            data_alvo = datetime.strptime(args.data, "%Y-%m-%d").date()
        except ValueError:
            log.error("Formato de data inválido. Use YYYY-MM-DD.")
            sys.exit(1)
        executar(data_alvo)
    elif args.agendar:
        iniciar_agendador()
    else:
        executar()
