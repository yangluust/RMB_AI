# Equilibrium working document

**Project:** RMB_AI (Yang Lu).
**Source:** [`intervention-incomplete-information-source.md`](intervention-incomplete-information-source.md),
"Intervention under Incomplete Information: Fixing Responsiveness and Persistent Policy Errors".
The source file is the owner's original, kept byte-for-byte
(SHA-256 `560b2ee1a7189ed8183dcc5ec8d6db9292f85ac645351bfe65a0a3292cad5960`).

This is the single cumulative working document of the project. The work is done in
four stages, and each stage adds one section here:

1. the equilibrium, written as implementable mathematical conditions (from Section 1 of
   the source and the lagged-price rule, equation (12), of its Section 4);
2. the pseudocode for those conditions;
3. the Python code for that pseudocode;
4. the parameter configuration, the computation and the results.

A section is in this document only once its stage has been carried out. After every
stage the work stops for the owner's review; the next stage starts only after the owner
approves the reviewed version. An approved section is not edited by a later stage.

Each section is enclosed by two marker lines (HTML comments, not shown when the file is
rendered): `BEGIN STAGE n: <stage name>` and `END STAGE n: <stage name>`.
