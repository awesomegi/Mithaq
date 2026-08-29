from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.permissions import IsAuthenticated, AllowAny

from django.shortcuts import get_object_or_404, redirect, render
from django.core.exceptions import PermissionDenied  # (added by ghadi: to catch subscription limit errors from check_contract_limit)
from notifications.services import NotificationService
from notifications.models import Notification
from contracts.models import Contract, ContractParty, ContractVersion
from contracts.serializers import (ContractSerializer, ContractCreateSerializer, ContractVersionSerializer)
from contracts.permissions import (IsContractParty, IsContractCreator, CanEditClauses, CanSign)
from contracts.services.contract_workflow import ContractWorkflowService
from contracts.services.signing_service import SigningService
from signatures.models import Signature
from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.db import transaction
from django.http import HttpRequest, HttpResponse
from invitations.models import SigningInvitation
from invitations.services import SigningInvitationService
# WeasyPrint requires GTK system libraries (libgobject, Pango) at import time.
# Wrapped in try/except so the server starts even if those libraries aren't
# installed yet. PDF export endpoints will return 503 until Railway installs them.
try:
    from weasyprint import HTML as WeasyHTML
except OSError:
    WeasyHTML = None
from .models import Contract, ContractParty, ContractVersion, ContractInvitedParty

# ══════════════════════════════════════════════════════════════
#  Template Views
# ══════════════════════════════════════════════════════════════


def contract_create_view(request: HttpRequest):
    return render(request, 'contracts/contract_create.html')


@login_required(login_url='accounts:sign_in')
def contract_list_view(request: HttpRequest):
    # Filters
    status_filter = request.GET.get('status', '')
    type_filter   = request.GET.get('type', '')
    date_filter   = request.GET.get('date', 'newest')

    STATUS_MAP = {
        'draft':             Contract.Status.DRAFT,
        'pending_signature': Contract.Status.PENDING_SIGNATURES,
        'signed':            Contract.Status.SIGNED,
        'completed':         Contract.Status.COMPLETED,
        'cancelled':         Contract.Status.CANCELLED,
    }

    # All contracts for this user
    qs = Contract.objects.filter(
        Q(creator=request.user) | Q(parties__user=request.user)
    ).distinct()

    # Apply filters
    if status_filter and status_filter in STATUS_MAP:
        qs = qs.filter(status=STATUS_MAP[status_filter])

    if type_filter == 'created':
        qs = qs.filter(creator=request.user)
    elif type_filter == 'received':
        qs = qs.exclude(creator=request.user)

    qs = qs.order_by('created_at' if date_filter == 'oldest' else '-created_at')

    return render(request, 'contracts/contract_list.html', {
        'contracts':       qs,
        'selected_status': status_filter,
        'selected_type':   type_filter,
        'selected_date':   date_filter,
    })



@login_required(login_url='accounts:sign_in')
def contract_detail_view(request: HttpRequest, pk):
    contract = get_object_or_404(Contract, pk=pk)

    # Find the invitation that links this user to this contract
    # (either as the sender or as an invitee)
    invitation = (
        SigningInvitation.objects.filter(contract=contract)
        .filter(Q(invited_by=request.user) | Q(invitee_user=request.user))
        .first()
    )

    if invitation:
        return redirect('invitations:invitation_contract_detail', invitation_id=invitation.pk)

    # Creator with no invitations yet — fall back to the contracts list
    return redirect('invitations:my_contracts')


def version_history_view(request: HttpRequest, pk):
    contract = get_object_or_404(Contract, pk=pk)

    user_party = ContractParty.objects.filter(
        contract=contract, user=request.user
    ).first()

    if not user_party:
        from django.http import Http404
        raise Http404

    versions = contract.versions.prefetch_related('clauses').order_by('-version_number')

    return render(request, 'contracts/version_history.html', {
        'contract': contract,
        'versions': versions,
    })


def audit_timeline_view(request: HttpRequest, pk):
    contract = get_object_or_404(Contract, pk=pk)

    user_party = ContractParty.objects.filter(
        contract=contract, user=request.user
    ).first()

    if not user_party:
        from django.http import Http404
        raise Http404

    from audit.models import AuditEvent
    events = AuditEvent.objects.filter(
        contract=contract
    ).select_related('actor').order_by('-created_at')

    return render(request, 'contracts/audit_timeline.html', {
        'contract': contract,
        'events':   events,
    })


# ══════════════════════════════════════════════════════════════
#  API Views
# ══════════════════════════════════════════════════════════════

def _as_bool(value):
    return value in [True, "true", "True", "1", 1, "on"]

class ContractListCreateView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        from django.contrib.auth import get_user_model
        User = get_user_model()

        user = request.user if request.user.is_authenticated else User.objects.first()

        invite_parties = request.data.get("invite_parties", [])

        serializer = ContractCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        # (added by ghadi: check_contract_limit() inside create_contract raises
        #  PermissionDenied if the user has no subscription or hit their plan limit)
        try:
            with transaction.atomic():
                contract = ContractWorkflowService.create_contract(
                    creator=user,
                    data=serializer.validated_data,
                )
        except PermissionDenied as exc:
            # Atomic block rolled back — safe to write notification now
            NotificationService.notify(
                user=user,
                notification_type=Notification.CONTRACT_LIMIT_REACHED,
            )
            return Response({'error': str(exc)}, status=status.HTTP_403_FORBIDDEN)

        for index, party in enumerate(invite_parties, start=1):
            invited_party = ContractInvitedParty.objects.create(
                contract=contract,
                title=party.get("title", "").strip(),
                full_name=party.get("full_name", "").strip(),
                mobile=party.get("mobile", "").strip(),
                phone=party.get("phone", "").strip(),
                email=party.get("email", "").strip(),

                party_type=party.get("party_type", "INDIVIDUAL"),
                signing_role=party.get("signing_role", "SIGNER"),

                national_id=party.get("national_id", "").strip(),
                nationality=party.get("nationality", "").strip(),
                address_country=party.get("address_country", "").strip(),
                address_city=party.get("address_city", "").strip(),

                organization_name=party.get("organization_name", "").strip(),
                commercial_registration=party.get("commercial_registration", "").strip(),
                tax_number=party.get("tax_number", "").strip(),

                can_view_contract=_as_bool(party.get("can_view_contract", True)),
                can_comment=_as_bool(party.get("can_comment", False)),
                can_edit=_as_bool(party.get("can_edit", False)),
                can_upload_files=_as_bool(party.get("can_upload_files", False)),
                can_sign=_as_bool(party.get("can_sign", True)),

                signing_order=int(party.get("signing_order") or index),
                invitation_message=party.get("invitation_message", "").strip(),
            )

            print("PARTY DATA:", party)
            print("RAW CAN EDIT:", party.get("can_edit"))
            invitation, secret = SigningInvitation.create_invitation(
                contract=contract,
                invited_by=user,
                signer_full_name=party.get("full_name", "").strip(),
                signer_mobile=party.get("mobile", "").strip(),
                signer_email=party.get("email", "").strip(),
                party_type=party.get("party_type", SigningInvitation.PartyType.INDIVIDUAL),
                contract_role=party.get("contract_role", SigningInvitation.ContractRole.SECOND_PARTY),
                signing_role=party.get("signing_role", SigningInvitation.SigningRole.SIGNER),
                signer_national_id=party.get("national_id", "").strip(),
                signer_nationality=party.get("nationality", "").strip(),
                organization_name=party.get("organization_name", "").strip(),
                commercial_registration=party.get("commercial_registration", "").strip(),
                tax_number=party.get("tax_number", "").strip(),
                can_view_contract=_as_bool(party.get("can_view_contract", True)),
                can_comment=_as_bool(party.get("can_comment", False)),
                can_edit=_as_bool(party.get("can_edit", False)),
                can_upload_files=_as_bool(party.get("can_upload_files", False)),
                can_sign=_as_bool(party.get("can_sign", True)),
                signing_order=int(party.get("signing_order") or index),
                invitation_message=party.get("invitation_message", "").strip(),
            )

            invited_party.invitation = invitation
            invited_party.save(update_fields=["invitation"])

            SigningInvitationService.send_existing_invitation(invitation, secret)

        # Notify the creator that their contract was created and invitations sent
        print(f"[Contract created] id={contract.id} — sending CONTRACT_CREATED to creator user_id={user.id}")
        NotificationService.notify(
            user=user,
            notification_type=Notification.CONTRACT_CREATED,
            contract=contract,
        )

        first_invitation = contract.signing_invitations.first()

        return Response({
            "id": str(contract.id),
            "invitation_id": str(first_invitation.id) if first_invitation else None,
        }, status=status.HTTP_201_CREATED)
        
'''
class ContractListCreateView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        contracts = Contract.objects.filter(
            parties__user=request.user
        ).select_related(
            'creator', 'current_version'
        ).prefetch_related(
            'parties__user', 'current_version__clauses', 'signatures__signer'
        ).order_by('-created_at')

        serializer = ContractSerializer(contracts, many=True)
        return Response(serializer.data)

    def post(self, request):
        serializer = ContractCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        contract = ContractWorkflowService.create_contract(
            creator=request.user,
            data=serializer.validated_data,
        )

        # Notify all parties that a new contract has been received
        NotificationService.notify_all_parties(
            contract=contract,
            notification_type=Notification.CONTRACT_RECEIVED,
            exclude_user=request.user,
        )

        return Response(
            ContractSerializer(contract).data,
            status=status.HTTP_201_CREATED
        )
'''

class ContractDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def get_contract(self, request, pk):
        contract = get_object_or_404(Contract, pk=pk)
        self.check_object_permissions(request, contract)
        return contract

    def get(self, request, pk):
        self.permission_classes = [IsAuthenticated, IsContractParty]
        contract = self.get_contract(request, pk)
        return Response(ContractSerializer(contract).data)

    def patch(self, request, pk):
        self.permission_classes = [IsAuthenticated, IsContractParty, CanEditClauses]
        contract = self.get_contract(request, pk)

        allowed = ['title_ar', 'title_en', 'description_ar', 'description_en']
        data = {k: v for k, v in request.data.items() if k in allowed}

        serializer = ContractSerializer(contract, data=data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()

        return Response(ContractSerializer(contract).data)

    def delete(self, request, pk):
        self.permission_classes = [IsAuthenticated, IsContractCreator, CanEditClauses]
        contract = self.get_contract(request, pk)
        contract.soft_delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class ApproveView(APIView):
    permission_classes = [IsAuthenticated, IsContractParty]

    def post(self, request, pk):
        contract = get_object_or_404(Contract, pk=pk)
        self.check_object_permissions(request, contract)

        party = get_object_or_404(ContractParty, contract=contract, user=request.user)
        ContractWorkflowService.approve_contract(contract, party)

        # Notify all parties that the contract has been accepted
        NotificationService.notify_all_parties(
            contract=contract,
            notification_type=Notification.CONTRACT_ACCEPTED,
            exclude_user=request.user,
        )

        contract.refresh_from_db()
        return Response(ContractSerializer(contract).data)


class SignView(APIView):
    permission_classes = [IsAuthenticated, CanSign]

    def post(self, request, pk):
        contract = get_object_or_404(Contract, pk=pk)
        self.check_object_permissions(request, contract)

        submitted_hash = request.data.get('hash')
        if not submitted_hash:
            return Response(
                {'error': 'الـ hash مطلوب'},
                status=status.HTTP_400_BAD_REQUEST
            )

        SigningService.validate_and_sign(
            contract=contract,
            signer=request.user,
            submitted_hash=submitted_hash,
            request=request,
        )

        contract.refresh_from_db()


        # Notify all parties that the contract has been signed
        NotificationService.notify_all_parties(
            contract=contract,
            notification_type=Notification.CONTRACT_SIGNED,
            exclude_user=request.user,
        )

        # If contract is completed notify all parties
        if contract.status == Contract.Status.COMPLETED:
            NotificationService.notify_all_parties(
                contract=contract,
                notification_type=Notification.CONTRACT_COMPLETED,
            )

        return Response(ContractSerializer(contract).data)


class CancelView(APIView):
    permission_classes = [IsAuthenticated, IsContractCreator]

    def post(self, request, pk):
        contract = get_object_or_404(Contract, pk=pk)
        self.check_object_permissions(request, contract)

        ContractWorkflowService.cancel_contract(contract, request.user)


        # Notify all parties that the contract has been rejected
        NotificationService.notify_all_parties(
            contract=contract,
            notification_type=Notification.CONTRACT_REJECTED,
            exclude_user=request.user,
        )

        contract.refresh_from_db()
        return Response(ContractSerializer(contract).data)


class VersionListView(APIView):
    permission_classes = [IsAuthenticated, IsContractParty]

    def get(self, request, pk):
        contract = get_object_or_404(Contract, pk=pk)
        self.check_object_permissions(request, contract)

        versions = contract.versions.prefetch_related('clauses').order_by('version_number')
        return Response(ContractVersionSerializer(versions, many=True).data)


class VersionDetailView(APIView):
    permission_classes = [IsAuthenticated, IsContractParty]

    def get(self, request, pk, version_number):
        contract = get_object_or_404(Contract, pk=pk)
        self.check_object_permissions(request, contract)

        version = get_object_or_404(
            ContractVersion,
            contract=contract,
            version_number=version_number
        )
        return Response(ContractVersionSerializer(version).data)
    

# Step 4: added by Remas — PDF generation view using WeasyPrint
# Takes the saved contract HTML from canonical_json and converts it to a downloadable PDF
def contract_pdf_view(request: HttpRequest, pk):
    # Get contract
    contract = get_object_or_404(Contract, pk=pk)

    # PDF only available after signing
    #if contract.status not in [Contract.Status.SIGNED, Contract.Status.COMPLETED]:
        #from django.http import HttpResponseForbidden
       # return HttpResponseForbidden("العقد لم يكتمل بعد")

    # Get HTML from canonical_json
    contract_html = contract.current_version.canonical_json.get('contract_html', '')

    # Logo URL
    logo_url = request.build_absolute_uri('/static/images/mithaq-logo.png')

    # Full PDF HTML
    full_html = f"""
    <!DOCTYPE html>
    <html lang="ar" dir="rtl">
    <head>
        <meta charset="UTF-8">
        <style>
            @page {{
                margin: 80px 50px 60px 50px;
                @top-left {{
                    content: url({logo_url});
                    vertical-align: middle;
                }}
                @bottom-left {{
                    content: url({logo_url});
                    vertical-align: middle;
                }}
                @bottom-center {{
                    content: "صفحة " counter(page) " من " counter(pages);
                    font-family: Arial, 'Times New Roman', sans-serif;
                    font-size: 9px;
                    color: #555;
                    vertical-align: middle;
                }}
            }}

            /* Global compact reset — kills large inline margins from contract_html */
            * {{
                margin-top: 0 !important;
                margin-bottom: 6px !important;
                line-height: 1.7;
            }}

            body {{
                font-family: Arial, 'Times New Roman', sans-serif;
                direction: rtl;
                color: #000;
                font-size: 13px;
                line-height: 1.7;
                margin: 0;
                padding: 0;
                background: #fff;
            }}

            p {{ margin: 4px 0 !important; }}
            h1, h2, h3, h4, h5 {{ margin: 12px 0 4px !important; }}
            li {{ margin-bottom: 4px !important; }}

            /* ── Header ── */
            .pdf-header {{
                display: flex;
                justify-content: space-between;
                align-items: center;
                border-bottom: 1px solid #B99655;
                padding-bottom: 14px;
                margin-bottom: 20px !important;
            }}
            .pdf-logo {{ height: 44px; }}
            .pdf-meta {{
                text-align: left;
                font-size: 11px;
                color: #333;
                line-height: 1.7;
            }}
            .pdf-meta p {{ margin: 2px 0 !important; }}

            /* ── Contract document wrapper ── */
            .contract-doc {{
                font-size: 13px;
                line-height: 1.7;
            }}

            /* ── Title block ── */
            .contract-title-block {{
                display: flex;
                flex-direction: column;
                text-align: center;
                margin-bottom: 14px !important;
                padding-bottom: 10px;
            }}
            .contract-bismillah {{
                order: 1;
                font-size: 15px;
                font-weight: bold;
                color: #000;
                margin: 0 0 4px 0 !important;
            }}
            .contract-title-block h2 {{
                order: 2;
                font-size: 17px;
                font-weight: bold;
                color: #000;
                margin: 0 0 4px 0 !important;
            }}
            .contract-title-en {{
                order: 3;
                font-size: 12px;
                color: #333;
                direction: ltr;
                text-align: center;
                margin: 2px 0 0 0 !important;
            }}

            /* ── Preamble ── */
            .contract-preamble {{
                padding: 8px 0;
                margin-bottom: 12px !important;
                font-size: 13px;
                line-height: 1.7;
            }}

            /* ── Articles ── */
            .contract-article {{
                margin-bottom: 12px !important;
                page-break-inside: avoid;
            }}
            .contract-article h4 {{
                font-size: 13px;
                font-weight: bold;
                color: #000;
                margin: 12px 0 4px !important;
            }}
            .contract-article p {{
                margin: 4px 0 !important;
            }}
            .contract-clauses-list {{
                padding-right: 24px;
                margin: 4px 0 !important;
            }}
            .contract-clauses-list li {{
                margin-bottom: 4px !important;
                line-height: 1.7;
            }}

            /* ── Signatures ── */
            .contract-signatures {{
                display: flex;
                gap: 24px;
                margin-top: 24px !important;
                padding-top: 16px;
                page-break-inside: avoid;
            }}
            .sig-block {{
                flex: 1;
                text-align: center;
            }}
            .sig-line {{
                border-bottom: 1px solid #000;
                margin: 30px 16px 8px !important;
            }}
            .sig-label {{
                font-size: 11px;
                color: #333;
                margin-bottom: 4px !important;
            }}
            .sig-name {{
                font-size: 12px;
                font-weight: bold;
                color: #000;
            }}

            /* ── Hash ── */
            .hash-box {{
                padding-top: 8px;
                font-size: 9px;
                color: #555;
                word-break: break-all;
                font-family: 'Courier New', Courier, monospace;
                direction: ltr;
                text-align: left;
                margin-top: 12px !important;
            }}

            /* ── Footer ── */
            .pdf-footer {{
                margin-top: 16px !important;
                border-top: 1px solid #ccc;
                padding-top: 8px;
                text-align: center;
                font-size: 10px;
                color: #555;
            }}
            .pdf-footer p {{ margin: 2px 0 !important; }}
        </style>
    </head>
    <body>
        <!-- Header -->
        <div class="pdf-header">
            <img src="{logo_url}" class="pdf-logo" alt="ميثاق">
            <div class="pdf-meta">
                <p>رقم العقد: {contract.id}</p>
                <p>تاريخ الإنشاء: {contract.created_at.strftime('%Y/%m/%d')}</p>
                <p>الحالة: {contract.get_status_display()}</p>
            </div>
        </div>

        <!-- Contract HTML -->
        {contract_html}

        <!-- Blockchain Hash -->
        {'<div class="hash-box">توثيق البلوكشين: ' + contract.canonical_hash + '</div>' if contract.canonical_hash else ''}

        <!-- Footer -->
        <div class="pdf-footer">
            <p>تم إنشاء هذا المستند بواسطة منصة ميثاق — جميع الحقوق محفوظة</p>
            <p>هذا العقد موثق رقمياً ومحمي بتقنية البلوكشين</p>
        </div>
    </body>
    </html>
    """

    # Convert to PDF
    if WeasyHTML is None:
        return HttpResponse(
            'خدمة تصدير PDF غير متاحة حالياً — مكتبات النظام المطلوبة غير مثبتة.',
            status=503,
            content_type='text/plain; charset=utf-8',
        )

    pdf = WeasyHTML(string=full_html, base_url=request.build_absolute_uri()).write_pdf()

    # Return PDF response
    response = HttpResponse(pdf, content_type='application/pdf')
    response['Content-Disposition'] = f'attachment; filename="mithaq-contract-{contract.id}.pdf"'
    return response