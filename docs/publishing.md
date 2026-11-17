# Wydania pykdpw

Paczka: `pykdpw`. Import: `pykdpw`. Komenda: `pykdpw`.
Jedynym źródłem numeru wersji jest `pykdpw/__init__.py`.
Tag wydania ma postać `v0.2.0` i musi odpowiadać wersji w kodzie.

## Kontrola przed wydaniem

W środowisku deweloperskim, w katalogu repozytorium:

```shell
python -m pip install -e ".[test,release]"
python -m pytest -q
python -m build
python -m twine check --strict dist/*
python scripts/check_distribution.py
```

Katalog `dist` powinien zawierać wyłącznie wheel i sdist bieżącej wersji.
`python -m build` buduje wheel ze świeżo utworzonej paczki źródłowej.
Skrypt sprawdza metadane i zawartość obu plików, instaluje wheel w nowym venv poza
repozytorium, uruchamia CLI i Python API oraz importuje syntetyczny raport.
Nie pobiera danych z KDPW. Instalacja zależności wymaga dostępu do PyPI.

Do wydania dołączamy tylko `pykdpw-VERSION-py3-none-any.whl` i
`pykdpw-VERSION.tar.gz`, bez baz, ZIP-ów KDPW, konfiguracji kont czy logów.

## GitHub Releases

Po poprawnym CI utwórz tag zgodny z numerem wersji i wydanie w
[GitHub Releases](https://github.com/solek13/kdpw/releases). Dołącz wheel i sdist
z przetestowanego `dist`. Nie zastępuj opublikowanych plików inną zawartością
pod tym samym numerem wersji.

Publiczny wheel można instalować bez konta GitHub i bez programu Git:

```shell
python -m pip install "https://github.com/solek13/kdpw/releases/download/v0.2.0/pykdpw-0.2.0-py3-none-any.whl"
```

## Jednorazowe powiązanie PyPI

Repozytorium zawiera workflow `.github/workflows/publish.yml`, korzystający z
[Trusted Publishing](https://docs.pypi.org/trusted-publishers/using-a-publisher/).
Nie potrzebuje stałego tokena ani hasła zapisanego w sekretach GitHuba.

Właściciel konta PyPI musi dodać publishera w
[ustawieniach publikowania konta](https://pypi.org/manage/account/publishing/).
Dla projektu, który jeszcze nie istnieje, wybierz „pending publisher”:

| Pole | Wartość |
| --- | --- |
| PyPI project name | `pykdpw` |
| Owner | `solek13` |
| Repository name | `kdpw` |
| Workflow name | `publish.yml` |
| Environment name | `pypi` |

[Dokumentacja pierwszej publikacji](https://docs.pypi.org/trusted-publishers/creating-a-project-through-oidc/).
Dodanie publishera nie rezerwuje nazwy na PyPI — projekt powstaje przy pierwszej
udanej publikacji. Jeśli projekt już istnieje i należy do Ciebie, dodaj te same
dane w ustawieniach Trusted Publishers tego projektu.

## Publikacja na PyPI

Po skonfigurowaniu publishera otwórz workflow
[Publish to PyPI](https://github.com/solek13/kdpw/actions/workflows/publish.yml),
wybierz **Run workflow** i podaj istniejący tag, np. `v0.2.0`.
Można również użyć GitHub CLI:

```shell
gh workflow run publish.yml --repo solek13/kdpw -f tag=v0.2.0
```

Workflow testuje kod z tego tagu, sprawdza zgodność numeru wersji, buduje paczki,
sprawdza instalację i przekazuje gotowe pliki do osobnego zadania publikacji.
Tylko zadanie publikacji ma uprawnienie `id-token: write` i środowisko `pypi`.
Uruchomienie workflow bez powiązania PyPI zakończy się odmową publikacji.

Po pierwszej udanej publikacji dostępna będzie krótka instalacja:

```shell
python -m pip install pykdpw
pykdpw fetch
```

Zweryfikuj stronę `https://pypi.org/project/pykdpw/` i instalację w świeżym venv.
Nie publikuj ponownie zmienionych plików pod istniejącą wersją; wydaj nową wersję.
