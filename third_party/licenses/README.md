# Third-Party License Materials

These files preserve upstream license text for locked dependencies whose package
metadata is ambiguous or whose distributed wheel omits the referenced license
file. They do not relicense Auris Flow or replace the complete release SBOM and
license inventory.

## ANTLR Python runtime 4.13.2

- Locked sdist SHA-256:
  `909b647e1d2fc2b70180ac586df3933e38919c85f98ccc656a96cd3f25ef3916`
- Upstream license source:
  `https://raw.githubusercontent.com/antlr/antlr4/4.13.2/LICENSE.txt`
- Preserved license text SHA-256:
  `3db1fb3ee79a4b4f9918fc4d0f6133bf18a3cf787f126cd22f8aa9b862281c0c`

The locked sdist was verified against `production/dagster/uv.lock`. Its Python
source headers identify the BSD 3-clause license but the archive omits the
referenced root `LICENSE.txt`, so the exact upstream tag text is retained here.

## python-dateutil 2.9.0.post0

- Locked universal wheel SHA-256:
  `a8b2bc7bffae282281c8140a97d3aa9c14da0b136dfe83f850eea9a5f7470427`
- Preserved license text SHA-256:
  `9313256b27c4a1b7666c433acbf70a447383df0ea8c4b59bee0b6e412a281f92`

The text is copied from the locked wheel's
`python_dateutil-2.9.0.post0.dist-info/LICENSE` with only a POSIX trailing
newline added. It states that the BSD license applies to all code, including
code also covered by Apache-2.0.
