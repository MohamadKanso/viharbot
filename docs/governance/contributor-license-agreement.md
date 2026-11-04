# ViharBot Contributor License Agreement (DRAFT)

**Version: 0.1-draft. Status: draft, not in force.**

> **This is a draft for the founder's review. It has not had legal review and must not be treated as enforceable.** It is not a contract, not legal advice and not a template whose sufficiency has been established. It binds no one until the founder has had it reviewed by counsel, approved it, published a version number without `-draft`, and set the status in [`CLA-ACCEPTANCES.md`](../../CLA-ACCEPTANCES.md) to `in-force`. Until then, external code is not merged (see [CONTRIBUTING](../../CONTRIBUTING.md#contribution-rights)). Text in **[DECISION: ...]** marks a choice the founder or counsel must make; remove each marker when it is settled.

This agreement is between you ("**Contributor**") and the owner of the ViharBot project ("**Owner**"), identified in [CONTRIBUTING](../../CONTRIBUTING.md) as the project's founder. It covers your contributions to the repository `vchokshi/viharbot`.

## 1. Definitions

* **Contribution**: any code, documentation, configuration, test, design or other material you submit to the project, for example through a pull request, and any later revision of it that you submit.
* **Project**: ViharBot and the repository `vchokshi/viharbot`, including any commercial product the Owner derives from it.

## 2. You keep your copyright

You do not transfer ownership of your Contribution. You keep the copyright and any other rights in it that this agreement does not grant. Nothing here presumes an assignment.

## 3. Copyright licence to the Owner

You grant the Owner a perpetual, worldwide, non-exclusive, royalty-free, irrevocable licence to use, reproduce, modify, prepare derivative works of, publicly display and perform, distribute and sublicense your Contribution and derivative works of it, in source or binary form, **under any licence terms the Owner chooses, including proprietary and commercial terms, and to relicense it later**.

This is the grant that lets the Project remain proprietary and become a commercial product. You are agreeing to it knowingly; the repository being publicly visible does not license anyone else to use the code.

**[DECISION: irrevocable versus revocable for future contributions only.]** Issue #56 asks for a revocation policy. As drafted, the licence is irrevocable for contributions already merged. Whether a contributor may withdraw acceptance for *future* contributions (which the gate already allows, by removing their row) is a choice for the founder.

## 4. Patent licence

You grant the Owner and recipients of the Project from the Owner a perpetual, worldwide, non-exclusive, royalty-free, irrevocable patent licence to make, have made, use, sell, offer to sell, import and otherwise transfer your Contribution, but only for patent claims you can license that are necessarily infringed by your Contribution alone or in combination with the Project at the time you submit it. If you start patent litigation alleging that the Project or a Contribution infringes a patent, the patent licences granted to you under this agreement end on the date you file it.

**[DECISION: counsel to confirm the termination wording.]**

## 5. What you confirm about your Contribution

You confirm that:

1. You are legally entitled to grant the licences above, and each Contribution is **your original work**, or you have clearly identified in the pull request any material that is not.
2. You have listed third-party code and dependencies, their licences and any compatibility concern in the pull request, as [CONTRIBUTING](../../CONTRIBUTING.md#contribution-rights) requires. You have not included code under a licence that would conflict with proprietary distribution, such as a copyleft licence, unless the Owner has agreed in writing.
3. **Your employer or another person has no rights in the Contribution**, or you have obtained a written waiver or authorisation from them, covering this Contribution, before submitting it. If your employer's policy or contract gives it rights in work you do, do not contribute without checking. **[DECISION: a separate corporate agreement for employer-owned contributions is not defined yet, as issue #56 notes.]**
4. The Contribution contains no secret, credential, personal data or confidential material, and you have checked AI-assisted output for copied code and licence obligations as CONTRIBUTING requires.

If any of this becomes untrue, tell the Owner promptly.

## 6. No obligation, no warranty

The Owner does not have to use, merge or credit your Contribution. You provide it **"as is"**, without warranty of any kind, except for the confirmations in section 5. You are not required to provide support.

## 7. Moral rights

To the extent the law allows, you waive, or agree not to assert, moral rights in your Contribution against the Owner and its licensees. Where a waiver is not permitted, you grant the Owner the rights it needs to use your Contribution as this agreement describes.

## 8. How you accept this agreement

Acceptance is by a recorded statement, not a signature on paper. In the pull request (or the issue it links), post a comment from your own GitHub account containing **only** this text, with the version that is currently in force:

```text
I have read the ViharBot Contributor License Agreement, version <VERSION>, and I agree to its terms for my contributions to vchokshi/viharbot.
```

The founder checks the comment and records your acceptance in [`CLA-ACCEPTANCES.md`](../../CLA-ACCEPTANCES.md), keyed by your numeric GitHub account ID. **Posting the comment is a request, not an acceptance on record.** The check described in [CLA acceptance](cla-acceptance.md) recognises you only after the founder-approved record is merged into `main`. Edit or delete your comment afterwards and the founder will see the record no longer matches; post a fresh comment instead.

Your acceptance covers Contributions you submit **after** it is recorded, for the version you accepted. A new version needs a new acceptance.

## 9. Changes to this agreement

The Owner may publish a new version. A new version does not alter the terms you accepted for Contributions you already submitted. **[DECISION: whether a minor version needs re-acceptance. As drafted, any version change does, because the gate matches the exact version.]**

## 10. General

**[DECISION: governing law, venue, entire-agreement and severability wording. Counsel to supply.]**

## Open questions for counsel

* Is a click-and-comment acceptance by a GitHub account sufficient evidence of agreement, in the Owner's jurisdiction and the Contributor's?
* Is a non-assignment licence with a relicensing right enough for a possible future commercial product, or is assignment needed?
* Contributors under 18, contributors acting for a company, and contributions by trusted contributors (whose terms are "not defined in this document yet" in CONTRIBUTING).
* Data protection: the acceptance record keeps a GitHub username, numeric ID, date and a link to the comment. Confirm that, and its retention, are acceptable.
