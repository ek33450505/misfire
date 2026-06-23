class Misfire < Formula
  include Language::Python::Virtualenv

  desc "Trace-grounded CLAUDE.md adherence auditor: which prose rules your agents ignore"
  homepage "https://github.com/ek33450505/misfire"
  url "https://files.pythonhosted.org/packages/42/e5/7fd68aea9a13ddcfa871363f3070f7f4d72e7b6486ae03296f3eba416c68/misfire-0.2.0.tar.gz"
  sha256 "276ea1c6b4db25965ec6025c61ae5eb98ecfe151a076924de869d8c16a1ce4c6"
  license "Apache-2.0"

  depends_on "python@3.12"

  # Core is stdlib-only (zero runtime dependencies) — no resource stanzas needed.
  def install
    virtualenv_install_with_resources
  end

  test do
    # Version surface doubles as the install smoke test. (The proof/ fixtures are
    # intentionally excluded from the published artifact, so there is no `misfire proof`
    # to assert against — unlike looptrip.)
    assert_match "misfire #{version}", shell_output("#{bin}/misfire --version")
  end
end
