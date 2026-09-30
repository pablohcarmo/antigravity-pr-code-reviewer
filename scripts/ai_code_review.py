import os
import sys
import json
import asyncio
import subprocess
import urllib.request
from pathlib import Path
from google.antigravity import Agent, LocalAgentConfig

DIFF_CHAR_LIMIT = 30000

def run_git_command(args: list[str], quiet: bool = False) -> str:
    """Executa comando git de forma segura com encoding resiliente. Lança exceção em caso de falha."""
    try:
        result = subprocess.run(
            args,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )
        return result.stdout.strip()
    except subprocess.CalledProcessError as e:
        error_msg = e.stderr.strip() if e.stderr else str(e)
        if not quiet:
            print(f"Erro ao executar {' '.join(args)}: {error_msg}", file=sys.stderr)
        raise RuntimeError(f"Falha ao executar comando Git: {' '.join(args)}") from e

def get_git_diff(base_ref: str) -> tuple[str, str]:
    """Obtém o diff e as estatísticas dos arquivos modificados com fallback resiliente de referências."""
    clean_base = base_ref.removeprefix("refs/heads/").removeprefix("origin/").strip() if base_ref else "main"
    
    # Lista de alvos a tentar em ordem de preferência
    candidates = [
        f"origin/{clean_base}...HEAD",
        f"origin/{clean_base}..HEAD",
        f"{clean_base}...HEAD",
        f"{clean_base}..HEAD",
        "HEAD~1..HEAD",
        "HEAD"
    ]
    
    for target in candidates:
        try:
            diff_stat = run_git_command(["git", "diff", "--stat", target, "--"], quiet=True)
            diff_text = run_git_command(["git", "diff", target, "--"], quiet=True)
            return diff_stat, diff_text
        except RuntimeError:
            continue

    # Fallback para alterações no working tree ou repositório sem commits anteriores
    try:
        diff_stat = run_git_command(["git", "diff", "--stat", "--"], quiet=True)
        diff_text = run_git_command(["git", "diff", "--"], quiet=True)
        return diff_stat, diff_text
    except RuntimeError:
        return "", ""

def load_system_instructions() -> str:
    """Lê as diretrizes de AGENTS.md no repo de destino ou usa o padrão da action."""
    base_instructions = (
        "Você é um engenheiro de software sênior realizando code review em um Pull Request no GitHub.\n"
        "Analise o diff fornecido, identifique riscos de bugs, segurança e oportunidades de melhoria.\n"
        "Formate a resposta em Markdown com resumo executivo, pontos de atenção e sugestões de código.\n"
    )
    agents_path_env = os.getenv("AGENTS_FILE")
    target_path = Path(agents_path_env) if agents_path_env else Path("AGENTS.md")
    fallback_path = Path(__file__).resolve().parent.parent / "AGENTS.md"

    if target_path.is_file():
        guidelines = target_path.read_text(encoding="utf-8")
        return f"{base_instructions}\n\nDiretrizes Operacionais do Projeto:\n{guidelines}"
    elif fallback_path.is_file():
        guidelines = fallback_path.read_text(encoding="utf-8")
        return f"{base_instructions}\n\nDiretrizes Operacionais Padrão:\n{guidelines}"
    return base_instructions

def get_pr_discussions() -> str:
    """Busca o histórico recente de comentários e respostas de desenvolvedores no PR via API do GitHub."""
    token = os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN")
    pr_number = os.getenv("PR_NUMBER")
    repo = os.getenv("GITHUB_REPOSITORY")

    if not token or not pr_number or not repo:
        return ""

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "Antigravity-PR-Reviewer"
    }

    discussions: list[str] = []

    # 1. Comentários de threads de review no código (/pulls/{pr}/comments)
    try:
        url_review = f"https://api.github.com/repos/{repo}/pulls/{pr_number}/comments"
        req_review = urllib.request.Request(url_review, headers=headers)
        with urllib.request.urlopen(req_review, timeout=10) as resp:
            review_comments = json.loads(resp.read().decode("utf-8"))
            for c in review_comments[-15:]:
                author = c.get("user", {}).get("login", "autor")
                path = c.get("path", "")
                line = c.get("line") or c.get("original_line") or ""
                body = c.get("body", "").strip()
                loc = f" em `{path}:{line}`" if path else ""
                discussions.append(f"- [@{author}{loc}]: {body}")
    except Exception as e:
        print(f"Aviso: não foi possível carregar comentários de review do PR ({e})", file=sys.stderr)

    # 2. Comentários gerais na timeline do PR (/issues/{pr}/comments)
    try:
        url_issue = f"https://api.github.com/repos/{repo}/issues/{pr_number}/comments"
        req_issue = urllib.request.Request(url_issue, headers=headers)
        with urllib.request.urlopen(req_issue, timeout=10) as resp:
            issue_comments = json.loads(resp.read().decode("utf-8"))
            for c in issue_comments[-10:]:
                author = c.get("user", {}).get("login", "autor")
                body = c.get("body", "").strip()
                # Ignora reviews anteriores para não poluir o prompt com diffs replicados
                if "Changes recommended" not in body and "Changes approved" not in body and "Resumo Executivo" not in body and len(body) < 2000:
                    discussions.append(f"- [@{author} na discussão geral]: {body}")
    except Exception as e:
        print(f"Aviso: não foi possível carregar comentários da issue ({e})", file=sys.stderr)

    if not discussions:
        return ""

    return (
        "Histórico de Comentários e Respostas de Desenvolvedores no PR:\n"
        + "\n".join(discussions)
        + "\n\n"
        + "DIRETRIZ DE AVALIAÇÃO:\n"
        + "Considere atentamente as justificativas e comentários acima. Se o desenvolvedor esclareceu "
        + "decisões motivadas por regras de negócio, prazos ou restrições do PMO/empresa, pondere essa informação "
        + "e não repita apontamentos dogmáticos já esclarecidos, adaptando sua recomendação de forma pragmática."
    )

def prepare_diff_prompt(diff_stat: str, diff_text: str, pr_discussions: str = "") -> str:
    """Monta o prompt incluindo estatísticas, truncamento defensivo e discussões do PR."""
    header = f"Resumo dos arquivos alterados:\n```text\n{diff_stat}\n```\n\n"
    discussions_block = f"{pr_discussions}\n\n" if pr_discussions else ""
    
    if len(diff_text) <= DIFF_CHAR_LIMIT:
        return f"Analise o seguinte git diff e faça uma revisão de código:\n\n{header}{discussions_block}```diff\n{diff_text}\n```"

    cutoff = diff_text.rfind("\n", 0, DIFF_CHAR_LIMIT)
    cutoff = cutoff if cutoff != -1 else DIFF_CHAR_LIMIT
    truncated = diff_text[:cutoff]

    return (
        "Analise o seguinte git diff e faça uma revisão de código.\n"
        "AVISO: O diff excedeu o limite máximo e foi truncado abaixo.\n\n"
        f"{header}{discussions_block}```diff\n{truncated}\n```\n\n"
        "[... diff truncado por limite de tamanho ...]"
    )

async def run_review():
    api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    if not api_key:
        print("GEMINI_API_KEY não configurada. Review ignorado.")
        return

    # Garante que ambas as variáveis de ambiente fiquem disponíveis para o SDK
    os.environ["GEMINI_API_KEY"] = api_key
    os.environ["GOOGLE_API_KEY"] = api_key

    base_ref = os.getenv("GITHUB_BASE_REF", "main")

    try:
        diff_stat, diff_output = get_git_diff(base_ref)
    except Exception as e:
        print(f"Falha na extração do diff: {e}", file=sys.stderr)
        sys.exit(1)

    if not diff_output.strip():
        print("Nenhuma alteração de código encontrada para revisar.")
        return

    system_instructions = load_system_instructions()
    config_params = {
        "system_instructions": system_instructions,
        "api_key": api_key,
    }
    
    gemini_model = os.getenv("GEMINI_MODEL")
    if gemini_model:
        config_params["model"] = gemini_model

    config = LocalAgentConfig(**config_params)
    pr_discussions = get_pr_discussions()
    prompt = prepare_diff_prompt(diff_stat, diff_output, pr_discussions)

    final_review = ""
    max_attempts = 3
    base_delay = 3

    for attempt in range(max_attempts):
        try:
            # Instancia o agente a cada tentativa para garantir sessão 100% limpa
            async with Agent(config) as agent:
                response = await agent.chat(prompt)
                
                # Tenta obter o texto completo via método text() ou streaming
                if hasattr(response, "text") and callable(response.text):
                    candidate_text = (await response.text()).strip()
                else:
                    review_body = []
                    async for token in response:
                        review_body.append(token)
                    candidate_text = "".join(review_body).strip()
                
                if not candidate_text:
                    raise RuntimeError("O modelo retornou uma resposta vazia.")
                
                final_review = candidate_text
                break
        except Exception as e:
            wait_time = base_delay * (2 ** attempt)
            if attempt < max_attempts - 1:
                print(f"Tentativa {attempt + 1} falhou ({e}). Tentando novamente em {wait_time}s...", file=sys.stderr)
                await asyncio.sleep(wait_time)
            else:
                print(f"Todas as {max_attempts} tentativas falharam: {e}", file=sys.stderr)
                raise e

    Path("review_output.md").write_text(final_review, encoding="utf-8")
    print("Revisão gerada com sucesso em review_output.md.")

if __name__ == "__main__":
    asyncio.run(run_review())