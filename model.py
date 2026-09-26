from django.db import models, transaction
from django.db.models import Sum, Q, F
from django.core.exceptions import ValidationError
from django.contrib.auth.models import User

class Warehouse(models.Model):
    name = models.CharField(max_length=120, unique=True)
    code = models.CharField(max_length=20, unique=True)
    address = models.TextField(blank=True, null=True)

    def __str__(self):
        return f"{self.name} [{self.code}]"


class Location(models.Model):
    LOCATION_TYPES = [
        ('internal', 'Internal Stock / Shelf'),
        ('vendor', 'Vendor (Virtual Source)'),
        ('customer', 'Customer (Virtual Destination)'),
        ('inventory_loss', 'Inventory Loss / Scrap'),
    ]
    warehouse = models.ForeignKey(Warehouse, on_delete=models.CASCADE, related_name='locations', null=True, blank=True)
    name = models.CharField(max_length=120)
    code = models.CharField(max_length=50, unique=True)
    location_type = models.CharField(max_length=20, choices=LOCATION_TYPES, default='internal')

    class Meta:
        ordering = ['code']

    def __str__(self):
        wh_name = self.warehouse.name if self.warehouse else "Virtual"
        return f"{wh_name} -> {self.name} ({self.get_location_type_display()})"


class ProductCategory(models.Model):
    name = models.CharField(max_length=100, unique=True)
    description = models.TextField(blank=True, null=True)

    class Meta:
        verbose_name_plural = "Product Categories"

    def __str__(self):
        return self.name


class Product(models.Model):
    name = models.CharField(max_length=200)
    sku = models.CharField(max_length=60, unique=True, db_index=True)
    category = models.ForeignKey(ProductCategory, on_delete=models.PROTECT, related_name='products')
    uom = models.CharField(max_length=30, default='Units', help_text="Unit of Measure (e.g., kg, units, boxes)")
    reorder_min_qty = models.DecimalField(max_digits=12, decimal_places=2, default=10.00, help_text="Threshold for low-stock alerts")
    created_at = models.DateTimeField(auto_now_add=True)

    def get_stock_at_location(self, location):
        incoming = StockMove.objects.filter(
            product=self,
            dest_location=location,
            operation__status='done'
        ).aggregate(total=Sum('quantity'))['total'] or 0

        outgoing = StockMove.objects.filter(
            product=self,
            source_location=location,
            operation__status='done'
        ).aggregate(total=Sum('quantity'))['total'] or 0

        return incoming - outgoing

    @property
    def total_stock(self):
        """Calculates net quantity across all physical/internal warehouse locations."""
        incoming = StockMove.objects.filter(
            product=self,
            dest_location__location_type='internal',
            operation__status='done'
        ).aggregate(total=Sum('quantity'))['total'] or 0

        outgoing = StockMove.objects.filter(
            product=self,
            source_location__location_type='internal',
            operation__status='done'
        ).aggregate(total=Sum('quantity'))['total'] or 0

        return incoming - outgoing

    @property
    def is_low_stock(self):
        return self.total_stock <= self.reorder_min_qty

    def __str__(self):
        return f"[{self.sku}] {self.name}"


class StockOperation(models.Model):
    OPERATION_TYPES = [
        ('receipt', 'Receipt (Incoming)'),
        ('delivery', 'Delivery (Outgoing)'),
        ('internal', 'Internal Transfer'),
        ('adjustment', 'Stock Adjustment'),
    ]
    STATUS_CHOICES = [
        ('draft', 'Draft'),
        ('waiting', 'Waiting Availability'),
        ('ready', 'Ready'),
        ('done', 'Done'),
        ('canceled', 'Canceled'),
    ]

    reference = models.CharField(max_length=64, unique=True, db_index=True)
    operation_type = models.CharField(max_length=20, choices=OPERATION_TYPES)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='draft')
    source_location = models.ForeignKey(Location, on_delete=models.PROTECT, related_name='outgoing_operations')
    dest_location = models.ForeignKey(Location, on_delete=models.PROTECT, related_name='incoming_operations')
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    notes = models.TextField(blank=True, null=True)

    def clean(self):
        if self.source_location == self.dest_location:
            raise ValidationError("Source and destination locations cannot be identical.")

        if self.operation_type == 'receipt' and self.source_location.location_type != 'vendor':
            raise ValidationError("Receipts must originate from a Vendor location.")
        if self.operation_type == 'delivery' and self.dest_location.location_type != 'customer':
            raise ValidationError("Delivery orders must terminate at a Customer location.")
        if self.operation_type == 'internal':
            if self.source_location.location_type != 'internal' or self.dest_location.location_type != 'internal':
                raise ValidationError("Internal transfers must move strictly between Internal locations.")

    @transaction.atomic
    def validate_and_execute(self):
        """Transitions operation to Done and ensures physical quantities do not run negative."""
        if self.status == 'done':
            return True

        self.clean()

        # Enforce inventory constraints on internal stock deductions
        if self.source_location.location_type == 'internal':
            for move in self.moves.select_related('product'):
                available_stock = move.product.get_stock_at_location(self.source_location)
                if available_stock < move.quantity:
                    raise ValidationError(
                        f"Insufficient stock for {move.product.name} at {self.source_location.name}. "
                        f"Available: {available_stock}, Required: {move.quantity}"
                    )

        # Finalize ledger movements
        self.moves.update(is_posted=True)
        self.status = 'done'
        self.save()
        return True

    def __str__(self):
        return f"{self.reference} ({self.get_operation_type_display()}) - {self.status.upper()}"


class StockMove(models.Model):
    """Immutable ledger record representing stock movement."""
    operation = models.ForeignKey(StockOperation, on_delete=models.CASCADE, related_name='moves')
    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name='stock_moves')
    quantity = models.DecimalField(max_digits=12, decimal_places=2)
    source_location = models.ForeignKey(Location, on_delete=models.PROTECT, related_name='moves_out')
    dest_location = models.ForeignKey(Location, on_delete=models.PROTECT, related_name='moves_in')
    timestamp = models.DateTimeField(auto_now_add=True)
    is_posted = models.BooleanField(default=False)

    def save(self, *args, **kwargs):
        # Auto-align move locations to the parent operation
        if not self.source_location_id:
            self.source_location = self.operation.source_location
        if not self.dest_location_id:
            self.dest_location = self.operation.dest_location
        if self.quantity <= 0:
            raise ValidationError("Movement quantity must be strictly greater than 0.")
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.quantity} {self.product.uom} of {self.product.name}"
