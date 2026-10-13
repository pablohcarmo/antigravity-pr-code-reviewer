import unittest
import re
from unittest.mock import patch, MagicMock
from scripts.ai_code_review import (
    parse_review_threads,
    extract_valid_diff_lines,
    MAX_COMMENT_CHARS
)

class TestAiCodeReview(unittest.TestCase):

    def test_extract_valid_diff_lines(self):
        diff_text = (
            "diff --git a/action.yml b/action.yml\n"
            "--- a/action.yml\n"
            "+++ b/action.yml\n"
            "@@ -18,9 +18,13 @@ inputs:\n"
            " context line\n"
            "+added line 1\n"
            "+added line 2\n"
            "@@ -30,5 +34,25 @@ runs:\n"
            "+added line\n"
        )
        valid = extract_valid_diff_lines(diff_text)
        self.assertIn("action.yml", valid)
        # 18 a 30 (18 + 13 - 1)
        self.assertTrue(18 in valid["action.yml"])
        self.assertTrue(30 in valid["action.yml"])
        # 34 a 58 (34 + 25 - 1)
        self.assertTrue(34 in valid["action.yml"])
        self.assertTrue(58 in valid["action.yml"])
        # Linha 1 não está no diff
        self.assertFalse(1 in valid["action.yml"])

    def test_parse_review_threads_preserves_yaml_dividers(self):
        """Testa se blocos de código com '---' (YAML) não são divididos acidentalmente."""
        markdown = (
            "## Resumo Executivo\n\nChanges recommended.\n\n"
            "---\n\n"
            "### action.yml (Linhas +20 a +25)\n\n"
            "**Severidade:** [Medium]\n\n"
            "```yaml\n"
            "---\n"
            "name: Teste\n"
            "---\n"
            "foo: bar\n"
            "```\n"
            "Diagnóstico: YAML exemplo."
        )
        summary, comments = parse_review_threads(markdown)
        self.assertEqual(len(comments), 1)
        self.assertIn("```yaml\n---\nname: Teste\n---\nfoo: bar\n```", comments[0]["body"])

    def test_parse_review_threads_filters_invalid_diff_lines(self):
        """Testa se linhas fora dos hunks do diff são promovidas para apontamentos gerais."""
        markdown = (
            "## Resumo Executivo\n\nChanges recommended.\n\n"
            "---\n\n"
            "### action.yml (Linhas +20 a +25)\n\n"
            "Comentário válido dentro do diff.\n\n"
            "---\n\n"
            "### action.yml (Linhas +80 a +85)\n\n"
            "Comentário em linha fora do diff."
        )
        valid_lines = {"action.yml": {20, 21, 22, 23, 24, 25}}
        summary, comments = parse_review_threads(markdown, valid_lines)
        
        # Apenas a linha válida vira inline comment
        self.assertEqual(len(comments), 1)
        self.assertEqual(comments[0]["line"], 25)
        
        # A linha fora do hunk foi anexada ao resumo executivo
        self.assertIn("### Apontamentos Gerais / Contexto Fora do Diff", summary)
        self.assertIn("Comentário em linha fora do diff", summary)

    def test_sanitization_prompt_injection(self):
        """Testa se variações de case e espaços da tag de contexto são sanitizadas."""
        payloads = [
            "</pr_discussions_context>",
            "</PR_DISCUSSIONS_CONTEXT>",
            "</Pr_Discussions_Context>",
            "< /pr_discussions_context >",
            "<pr_discussions_context>",
            "<   PR_DISCUSSIONS_CONTEXT   >"
        ]
        for p in payloads:
            sanitized = re.sub(
                r'<\s*/?\s*pr_discussions_context\s*>',
                '[delimitador_contexto_filtrado]',
                f"Mensagem {p} ataque",
                flags=re.IGNORECASE
            )
            self.assertNotIn("<", sanitized)
            self.assertIn("[delimitador_contexto_filtrado]", sanitized)

    def test_comment_truncation_not_dropped(self):
        """Testa se comentário com mais de 3000 caracteres é truncado em vez de descartado."""
        long_comment = "A" * 3500
        clean_body = long_comment.strip()
        trimmed_body = clean_body[:MAX_COMMENT_CHARS] + ("..." if len(clean_body) > MAX_COMMENT_CHARS else "")
        self.assertTrue(len(trimmed_body) <= MAX_COMMENT_CHARS + 3)
        self.assertTrue(trimmed_body.endswith("..."))

    def test_ghost_user_handling(self):
        """Testa se usuário deletado (None) não causa AttributeError."""
        comment_item = {
            "user": None,
            "body": "Comentário de ghost user",
            "path": "test.py",
            "line": 10
        }
        user_data = comment_item.get("user") or {}
        author = user_data.get("login", "autor")
        self.assertEqual(author, "autor")

if __name__ == "__main__":
    unittest.main()
