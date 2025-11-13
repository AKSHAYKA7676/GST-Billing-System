import datetime
import json
import num2words

from decimal import Decimal, ROUND_HALF_UP

from django.shortcuts import render, redirect, get_object_or_404
from django.http import JsonResponse
from django.db.models import Max

from django.contrib.auth.decorators import login_required
from django.contrib.auth import login
from django.contrib.auth.forms import AuthenticationForm, UserCreationForm

from .models import (
    Customer,
    Invoice,
    Product,
    UserProfile,
    Inventory,
    InventoryLog,
    Book,
    BookLog
)

from .utils import (
    invoice_data_validator,
    invoice_data_processor,
    update_products_from_invoice,
    update_inventory,
    create_inventory,
    add_customer_book,
    auto_deduct_book_from_invoice,
    remove_inventory_entries_for_invoice
)

from .forms import (
    CustomerForm,
    ProductForm,
    UserProfileForm,
    InventoryLogForm,
    BookLogForm
)

# =================================================================
# USER MANAGEMENT
# =================================================================

@login_required
def user_profile_edit(request):
    context = {}
    user_profile = get_object_or_404(UserProfile, user=request.user)
    context['user_profile_form'] = UserProfileForm(instance=user_profile)

    if request.method == "POST":
        user_profile_form = UserProfileForm(request.POST, instance=user_profile)
        user_profile_form.save()
        return redirect('user_profile')

    return render(request, 'gstbillingapp/user_profile_edit.html', context)


@login_required
def user_profile(request):
    user_profile = get_object_or_404(UserProfile, user=request.user)
    return render(request, 'gstbillingapp/user_profile.html', {"user_profile": user_profile})


def login_view(request):
    if request.user.is_authenticated:
        return redirect("invoice_create")

    context = {}
    auth_form = AuthenticationForm(request)

    if request.method == "POST":
        auth_form = AuthenticationForm(request, data=request.POST)
        if auth_form.is_valid():
            user = auth_form.get_user()
            if user:
                login(request, user)
                return redirect("invoice_create")
        else:
            context["error_message"] = auth_form.get_invalid_login_error()

    context["auth_form"] = auth_form
    return render(request, 'gstbillingapp/login.html', context)


def signup_view(request):
    if request.user.is_authenticated:
        return redirect("invoice_create")

    context = {}
    signup_form = UserCreationForm()
    profile_edit_form = UserProfileForm()

    context["signup_form"] = signup_form
    context["profile_edit_form"] = profile_edit_form

    if request.method == "POST":
        signup_form = UserCreationForm(request.POST)
        profile_edit_form = UserProfileForm(request.POST)

        context["signup_form"] = signup_form
        context["profile_edit_form"] = profile_edit_form

        if signup_form.is_valid():
            user = signup_form.save()
        else:
            context["error_message"] = signup_form.errors
            return render(request, 'gstbillingapp/signup.html', context)

        if profile_edit_form.is_valid():
            userprofile = profile_edit_form.save(commit=False)
            userprofile.user = user
            userprofile.save()
            login(request, user, backend='django.contrib.auth.backends.ModelBackend')
            return redirect("invoice_create")

    return render(request, 'gstbillingapp/signup.html', context)


# =================================================================
# INVOICE CREATION / LIST / DELETE
# =================================================================

@login_required
def invoice_create(request):

    # If business is not set → redirect to profile page
    user_profile = get_object_or_404(UserProfile, user=request.user)
    if not user_profile.business_title:
        return redirect('user_profile_edit')

    context = {}
    # default invoice number
    last_no = Invoice.objects.filter(user=request.user).aggregate(Max('invoice_number'))['invoice_number__max']
    context['default_invoice_number'] = (last_no + 1) if last_no else 1
    context['default_invoice_date'] = datetime.datetime.strftime(datetime.datetime.now(), '%Y-%m-%d')

    if request.method == 'POST':
        invoice_data = request.POST

        validation_error = invoice_data_validator(invoice_data)
        if validation_error:
            context["error_message"] = validation_error
            return render(request, 'gstbillingapp/invoice_create.html', context)

        invoice_data_processed = invoice_data_processor(invoice_data)

        # ------------------------------------
        # CUSTOMER
        # ------------------------------------
        try:
            customer = Customer.objects.get(
                user=request.user,
                customer_name=invoice_data['customer-name'],
                customer_address=invoice_data['customer-address'],
                customer_phone=invoice_data['customer-phone'],
                customer_gst=invoice_data['customer-gst']
            )
        except:
            customer = Customer(
                user=request.user,
                customer_name=invoice_data['customer-name'],
                customer_address=invoice_data['customer-address'],
                customer_phone=invoice_data['customer-phone'],
                customer_gst=invoice_data['customer-gst']
            )
            customer.save()
            add_customer_book(customer)

        # UPDATE PRODUCTS
        update_products_from_invoice(invoice_data_processed, request)

        # SAVE INVOICE
        invoice_json = json.dumps(invoice_data_processed)
        new_invoice = Invoice(
            user=request.user,
            invoice_number=int(invoice_data['invoice-number']),
            invoice_date=datetime.datetime.strptime(invoice_data['invoice-date'], '%Y-%m-%d'),
            invoice_customer=customer,
            invoice_json=invoice_json
        )
        new_invoice.save()

        # INVENTORY
        update_inventory(new_invoice, request)
        auto_deduct_book_from_invoice(new_invoice)

        return redirect('invoice_viewer', invoice_id=new_invoice.id)

    return render(request, 'gstbillingapp/invoice_create.html', context)


@login_required
def invoices(request):
    context = {
        "invoices": Invoice.objects.filter(user=request.user).order_by('-id')
    }
    return render(request, 'gstbillingapp/invoices.html', context)


# =====================================================================
#  NEW GST SYSTEM 2.0 — INVOICE VIEWER (REPLACED & UPDATED)
# =====================================================================

@login_required
def invoice_viewer(request, invoice_id):
    """
    GST BILLING SYSTEM 2.0
    Completely rewritten invoice view:
    ✓ CGST/SGST/IGST auto-detection
    ✓ HSN digit recommendations (B2B=6, B2C=4)
    ✓ Proper breakup & totals
    ✓ Flexible invoice_json reading
    """
    invoice_obj = get_object_or_404(Invoice, user=request.user, id=invoice_id)
    user_profile = get_object_or_404(UserProfile, user=request.user)

    # -----------------------------
    # LOAD STORED JSON
    # -----------------------------
    try:
        invoice_data = json.loads(invoice_obj.invoice_json or "{}")
    except:
        invoice_data = {}

    # -----------------------------
    # BASIC SETUP
    # -----------------------------
    seller_gst = (user_profile.business_gst or "").strip()

    buyer_gst = ""
    if invoice_obj.invoice_customer and invoice_obj.invoice_customer.customer_gst:
        buyer_gst = invoice_obj.invoice_customer.customer_gst.strip()
    else:
        buyer_gst = invoice_data.get("customer_gst", "").strip()

    # Invoice Type
    invoice_type = "B2B" if buyer_gst else "B2C"

    # -----------------------------
    # TAX MODE (INTRA vs INTER)
    # -----------------------------
    def gst_state(gstin):
        if gstin and len(gstin) >= 2 and gstin[:2].isdigit():
            return gstin[:2]
        return None

    s_state = gst_state(seller_gst)
    b_state = gst_state(buyer_gst)

    tax_mode = "INTRA" if (s_state and b_state and s_state == b_state) else "INTER"

    # -----------------------------
    # ITEMS EXTRACTION
    # -----------------------------
    items = invoice_data.get("invoice_items") or invoice_data.get("items") or []

    breakdown = []
    total_taxable = Decimal("0.00")
    total_tax = Decimal("0.00")
    total_cgst = Decimal("0.00")
    total_sgst = Decimal("0.00")
    total_igst = Decimal("0.00")

    for it in items:
        desc = it.get("description") or ""
        qty = Decimal(str(it.get("qty") or 1))
        rate = Decimal(str(it.get("rate") or 0))
        tax_percent = Decimal(str(it.get("tax_rate") or 0))
        hsn = it.get("hsn") or ""

        taxable_value = (qty * rate).quantize(Decimal("0.01"), ROUND_HALF_UP)
        tax_value = (taxable_value * tax_percent / Decimal("100")).quantize(Decimal("0.01"), ROUND_HALF_UP)

        cgst = sgst = igst = Decimal("0.00")

        if tax_mode == "INTRA":
            cgst = (tax_value / 2).quantize(Decimal("0.01"), ROUND_HALF_UP)
            sgst = (tax_value / 2).quantize(Decimal("0.01"), ROUND_HALF_UP)
            total_cgst += cgst
            total_sgst += sgst
        else:
            igst = tax_value
            total_igst += igst

        total_taxable += taxable_value
        total_tax += tax_value

        # HSN DIGIT CHECK (GST 2.0 RULE)
        hsn_digits = "".join(ch for ch in hsn if ch.isdigit())
        recommended = 6 if invoice_type == "B2B" else 4

        breakdown.append({
            "description": desc,
            "qty": qty,
            "rate": rate,
            "tax_percent": tax_percent,
            "taxable_value": taxable_value,
            "tax_value": tax_value,
            "cgst": cgst,
            "sgst": sgst,
            "igst": igst,
            "hsn": hsn,
            "hsn_warning": len(hsn_digits) < recommended,
            "hsn_recommended": recommended,
        })

    grand_total = (total_taxable + total_tax).quantize(Decimal("0.01"))

    # Total in words
    try:
        total_in_words = num2words.num2words(int(grand_total), lang="en_IN").title()
    except:
        total_in_words = ""

    return render(request, 'gstbillingapp/invoice_printer.html', {
        "invoice": invoice_obj,
        "invoice_data": invoice_data,
        "breakdown": breakdown,
        "total_taxable": total_taxable,
        "total_tax": total_tax,
        "total_cgst": total_cgst,
        "total_sgst": total_sgst,
        "total_igst": total_igst,
        "grand_total": grand_total,
        "seller_gst": seller_gst,
        "buyer_gst": buyer_gst,
        "invoice_type": invoice_type,
        "tax_mode": tax_mode,
        "total_in_words": total_in_words,
        "user_profile": user_profile,
        "currency": "₹",
    })


# =====================================================================
# INVOICE DELETE
# =====================================================================

@login_required
def invoice_delete(request):
    if request.method == "POST":
        invoice_id = request.POST["invoice_id"]
        invoice_obj = get_object_or_404(Invoice, user=request.user, id=invoice_id)

        if len(request.POST.getlist('inventory-del')):
            remove_inventory_entries_for_invoice(invoice_obj, request.user)

        invoice_obj.delete()

    return redirect('invoices')


# =====================================================================
# CUSTOMERS
# =====================================================================

@login_required
def customers(request):
    return render(request, 'gstbillingapp/customers.html', {
        "customers": Customer.objects.filter(user=request.user)
    })


@login_required
def customersjson(request):
    customers = list(Customer.objects.filter(user=request.user).values())
    return JsonResponse(customers, safe=False)


@login_required
def customer_edit(request, customer_id):
    customer_obj = get_object_or_404(Customer, user=request.user, id=customer_id)

    if request.method == "POST":
        form = CustomerForm(request.POST, instance=customer_obj)
        if form.is_valid():
            form.save()
            return redirect('customers')

    return render(request, "gstbillingapp/customer_edit.html", {
        "customer_form": CustomerForm(instance=customer_obj)
    })


@login_required
def customer_add(request):
    if request.method == "POST":
        form = CustomerForm(request.POST)
        customer = form.save(commit=False)
        customer.user = request.user
        customer.save()
        add_customer_book(customer)
        return redirect('customers')

    return render(request, "gstbillingapp/customer_edit.html", {
        "customer_form": CustomerForm()
    })


@login_required
def customer_delete(request):
    if request.method == "POST":
        customer_obj = get_object_or_404(Customer, user=request.user, id=request.POST["customer_id"])
        customer_obj.delete()
    return redirect('customers')


# =====================================================================
# PRODUCTS
# =====================================================================

@login_required
def products(request):
    return render(request, 'gstbillingapp/products.html', {
        "products": Product.objects.filter(user=request.user)
    })


@login_required
def productsjson(request):
    products = list(Product.objects.filter(user=request.user).values())
    return JsonResponse(products, safe=False)


@login_required
def product_edit(request, product_id):
    product_obj = get_object_or_404(Product, user=request.user, id=product_id)

    if request.method == "POST":
        form = ProductForm(request.POST, instance=product_obj)
        if form.is_valid():
            form.save()
            return redirect('products')

    return render(request, "gstbillingapp/product_edit.html", {
        "product_form": ProductForm(instance=product_obj)
    })


@login_required
def product_add(request):
    if request.method == "POST":
        form = ProductForm(request.POST)
        if form.is_valid():
            product = form.save(commit=False)
            product.user = request.user
            product.save()
            create_inventory(product)
            return redirect('products')

    return render(request, "gstbillingapp/product_edit.html", {
        "product_form": ProductForm()
    })


@login_required
def product_delete(request):
    if request.method == "POST":
        product_obj = get_object_or_404(Product, user=request.user, id=request.POST["product_id"])
        product_obj.delete()

    return redirect('products')


# =====================================================================
# INVENTORY
# =====================================================================

@login_required
def inventory(request):
    return render(request, "gstbillingapp/inventory.html", {
        "inventory_list": Inventory.objects.filter(user=request.user),
        "untracked_products": Product.objects.filter(user=request.user, inventory=None)
    })


@login_required
def inventory_logs(request, inventory_id):
    inv = get_object_or_404(Inventory, id=inventory_id, user=request.user)
    logs = InventoryLog.objects.filter(user=request.user, product=inv.product).order_by('-id')

    return render(request, "gstbillingapp/inventory_logs.html", {
        "inventory": inv,
        "inventory_logs": logs
    })


@login_required
def inventory_logs_add(request, inventory_id):
    inventory = get_object_or_404(Inventory, id=inventory_id, user=request.user)

    if request.method == "POST":
        form = InventoryLogForm(request.POST)
        invoice_no = request.POST.get("invoice_no")
        invoice = None

        if invoice_no:
            try:
                invoice = Invoice.objects.get(user=request.user, invoice_number=int(invoice_no))
            except:
                return render(request, "gstbillingapp/inventory_logs_add.html", {
                    "inventory": inventory,
                    "error_message": f"Incorrect invoice number {invoice_no}",
                    "form": form
                })

        log = form.save(commit=False)
        log.user = request.user
        log.product = inventory.product
        if invoice:
            log.associated_invoice = invoice
        log.save()

        inventory.current_stock += log.change
        inventory.last_log = log
        inventory.save()

        return redirect('inventory_logs', inventory.id)

    return render(request, "gstbillingapp/inventory_logs_add.html", {
        "inventory": inventory,
        "form": InventoryLogForm()
    })


# =====================================================================
# BOOKS
# =====================================================================

@login_required
def books(request):
    return render(request, "gstbillingapp/books.html", {
        "book_list": Book.objects.filter(user=request.user)
    })


@login_required
def book_logs(request, book_id):
    book = get_object_or_404(Book, id=book_id, user=request.user)
    logs = BookLog.objects.filter(parent_book=book).order_by('-id')

    return render(request, "gstbillingapp/book_logs.html", {
        "book": book,
        "book_logs": logs
    })


@login_required
def book_logs_add(request, book_id):
    book = get_object_or_404(Book, id=book_id, user=request.user)

    if request.method == "POST":
        form = BookLogForm(request.POST)
        invoice_no = request.POST.get("invoice_no")
        invoice = None

        if invoice_no:
            try:
                invoice = Invoice.objects.get(user=request.user, invoice_number=int(invoice_no))
            except:
                return render(request, "gstbillingapp/book_logs_add.html", {
                    "book": book,
                    "error_message": f"Incorrect invoice number {invoice_no}",
                    "form": form
                })

        log = form.save(commit=False)
        log.parent_book = book
        if invoice:
            log.associated_invoice = invoice
        log.save()

        book.current_balance += log.change
        book.last_log = log
        book.save()

        return redirect('book_logs', book.id)

    return render(request, "gstbillingapp/book_logs_add.html", {
        "book": book,
        "form": BookLogForm()
    })


# =====================================================================
# LANDING PAGE
# =====================================================================

def landing_page(request):
    return render(request, 'gstbillingapp/pages/landing_page.html')
