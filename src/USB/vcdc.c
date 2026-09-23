/*
 * Copyright (c) 2016, Devan Lai
 *
 * Permission to use, copy, modify, and/or distribute this software
 * for any purpose with or without fee is hereby granted, provided
 * that the above copyright notice and this permission notice
 * appear in all copies.
 *
 * THE SOFTWARE IS PROVIDED "AS IS" AND THE AUTHOR DISCLAIMS ALL
 * WARRANTIES WITH REGARD TO THIS SOFTWARE INCLUDING ALL IMPLIED
 * WARRANTIES OF MERCHANTABILITY AND FITNESS. IN NO EVENT SHALL THE
 * AUTHOR BE LIABLE FOR ANY SPECIAL, DIRECT, INDIRECT, OR
 * CONSEQUENTIAL DAMAGES OR ANY DAMAGES WHATSOEVER RESULTING FROM
 * LOSS OF USE, DATA OR PROFITS, WHETHER IN AN ACTION OF CONTRACT,
 * NEGLIGENCE OR OTHER TORTIOUS ACTION, ARISING OUT OF OR IN
 * CONNECTION WITH THE USE OR PERFORMANCE OF THIS SOFTWARE.
 */

#include <libopencm3/usb/usbd.h>
#include <libopencm3/usb/cdc.h>
#include "config.h"
#ifndef VCDC_UART_BRIDGE_AVAILABLE
#define VCDC_UART_BRIDGE_AVAILABLE 0
#endif
#if VCDC_UART_BRIDGE_AVAILABLE
#include <libopencm3/cm3/nvic.h>
#include <libopencm3/stm32/gpio.h>
#include <libopencm3/stm32/rcc.h>
#include <libopencm3/stm32/usart.h>
#endif
#include "composite_usb_conf.h"
#include "vcdc.h"

#if VCDC_AVAILABLE

/* Descriptor structures */
const struct cdc_acm_functional_descriptors vcdc_acm_functional_descriptors = {
    .header = {
        .bFunctionLength = sizeof(struct usb_cdc_header_descriptor),
        .bDescriptorType = CS_INTERFACE,
        .bDescriptorSubtype = USB_CDC_TYPE_HEADER,
        .bcdCDC = 0x0110,
    },
    .call_mgmt = {
        .bFunctionLength =
        sizeof(struct usb_cdc_call_management_descriptor),
        .bDescriptorType = CS_INTERFACE,
        .bDescriptorSubtype = USB_CDC_TYPE_CALL_MANAGEMENT,
        .bmCapabilities = 0,
        .bDataInterface = INTF_VCDC_DATA,
    },
    .acm = {
        .bFunctionLength = sizeof(struct usb_cdc_acm_descriptor),
        .bDescriptorType = CS_INTERFACE,
        .bDescriptorSubtype = USB_CDC_TYPE_ACM,
        .bmCapabilities = 0,
    },
    .cdc_union = {
        .bFunctionLength = sizeof(struct usb_cdc_union_descriptor),
        .bDescriptorType = CS_INTERFACE,
        .bDescriptorSubtype = USB_CDC_TYPE_UNION,
        .bControlInterface = INTF_VCDC_COMM,
        .bSubordinateInterface0 = INTF_VCDC_DATA,
    }
};

/* Input/output buffers */
static uint8_t vcdc_tx_buffer[VCDC_TX_BUFFER_SIZE];
static uint8_t vcdc_rx_buffer[VCDC_RX_BUFFER_SIZE];

static uint16_t vcdc_tx_head = 0;
static uint16_t vcdc_tx_tail = 0;

static uint16_t vcdc_rx_head = 0;
static uint16_t vcdc_rx_tail = 0;

_Static_assert((VCDC_RX_BUFFER_SIZE >= USB_VCDC_MAX_PACKET_SIZE),
               "RX buffer too small");

#define IS_POW_OF_TWO(X) (((X) & ((X)-1)) == 0)
_Static_assert(IS_POW_OF_TWO(VCDC_RX_BUFFER_SIZE),
               "Unmasked circular buffer size must be a power of two");
_Static_assert(IS_POW_OF_TWO(VCDC_TX_BUFFER_SIZE),
               "Unmasked circular buffer size must be a power of two");
_Static_assert(VCDC_TX_BUFFER_SIZE <= UINT16_MAX/2,
               "Buffer size too big for unmasked circular buffer");
_Static_assert(VCDC_RX_BUFFER_SIZE <= UINT16_MAX/2,
               "Buffer size too big for unmasked circular buffer");

static bool vcdc_tx_buffer_empty(void) {
    return vcdc_tx_head == vcdc_tx_tail;
}

static bool vcdc_tx_buffer_full(void) {
    return (uint16_t)(vcdc_tx_tail - vcdc_tx_head) == VCDC_TX_BUFFER_SIZE;
}

static void vcdc_tx_buffer_put(uint8_t data) {
    vcdc_tx_buffer[vcdc_tx_tail % VCDC_TX_BUFFER_SIZE] = data;
    vcdc_tx_tail++;
}

static uint8_t vcdc_tx_buffer_get(void) {
    uint8_t data = vcdc_tx_buffer[vcdc_tx_head % VCDC_TX_BUFFER_SIZE];
    vcdc_tx_head++;
    return data;
}

static bool vcdc_rx_buffer_empty(void) {
    return vcdc_rx_head == vcdc_rx_tail;
}

static bool vcdc_rx_buffer_full(void) {
    return (uint16_t)(vcdc_rx_tail - vcdc_rx_head) == VCDC_RX_BUFFER_SIZE;
}

static void vcdc_rx_buffer_put(uint8_t data) {
    vcdc_rx_buffer[vcdc_rx_tail % VCDC_RX_BUFFER_SIZE] = data;
    vcdc_rx_tail++;
}

static uint8_t vcdc_rx_buffer_get(void) {
    uint8_t data = vcdc_rx_buffer[vcdc_rx_head % VCDC_RX_BUFFER_SIZE];
    vcdc_rx_head++;
    return data;
}

size_t vcdc_recv_buffered(uint8_t* data, size_t max_bytes) {
    size_t bytes_read = 0;
    while (!vcdc_rx_buffer_empty() && (bytes_read < max_bytes)) {
        data[bytes_read++] = vcdc_rx_buffer_get();
    }

    return bytes_read;
}

size_t vcdc_send_buffered(const uint8_t* data, size_t num_bytes) {
    size_t bytes_queued = 0;
    while (!vcdc_tx_buffer_full() && bytes_queued < num_bytes) {
        vcdc_tx_buffer_put(data[bytes_queued++]);
    }

    return bytes_queued;
}

size_t vcdc_send_buffer_space(void) {
    return VCDC_TX_BUFFER_SIZE - (uint16_t)(vcdc_tx_tail - vcdc_tx_head);
}

/* User callbacks */
static GenericCallback vcdc_rx_callback = NULL;
static GenericCallback vcdc_tx_callback = NULL;

#if VCDC_UART_BRIDGE_AVAILABLE

/* USART1 <-> VCDC software FIFOs.  They absorb the difference between USB
 * frame timing and the UART byte rate. */
#define VCDC_UART_TX_BUFFER_SIZE 256
#define VCDC_UART_RX_BUFFER_SIZE 256

static volatile uint8_t vcdc_uart_tx_buffer[VCDC_UART_TX_BUFFER_SIZE];
static volatile uint8_t vcdc_uart_rx_buffer[VCDC_UART_RX_BUFFER_SIZE];
static volatile uint16_t vcdc_uart_tx_head;
static volatile uint16_t vcdc_uart_tx_tail;
static volatile uint16_t vcdc_uart_rx_head;
static volatile uint16_t vcdc_uart_rx_tail;

static struct usb_cdc_line_coding vcdc_line_coding = {
    .dwDTERate = DEFAULT_BAUDRATE,
    .bCharFormat = USB_CDC_1_STOP_BITS,
    .bParityType = USB_CDC_NO_PARITY,
    .bDataBits = 8
};

#define VCDC_UART_IS_POW_OF_TWO(X) (((X) & ((X)-1)) == 0)
_Static_assert(VCDC_UART_IS_POW_OF_TWO(VCDC_UART_TX_BUFFER_SIZE),
               "VCDC UART TX buffer must be a power of two");
_Static_assert(VCDC_UART_IS_POW_OF_TWO(VCDC_UART_RX_BUFFER_SIZE),
               "VCDC UART RX buffer must be a power of two");

static bool vcdc_uart_tx_empty(void) {
    return vcdc_uart_tx_head == vcdc_uart_tx_tail;
}

static bool vcdc_uart_tx_full(void) {
    return (uint16_t)(vcdc_uart_tx_tail - vcdc_uart_tx_head) ==
           VCDC_UART_TX_BUFFER_SIZE;
}

static bool vcdc_uart_rx_empty(void) {
    return vcdc_uart_rx_head == vcdc_uart_rx_tail;
}

static bool vcdc_uart_rx_full(void) {
    return (uint16_t)(vcdc_uart_rx_tail - vcdc_uart_rx_head) ==
           VCDC_UART_RX_BUFFER_SIZE;
}

static void vcdc_uart_reset_buffers(void) {
    vcdc_uart_tx_head = 0;
    vcdc_uart_tx_tail = 0;
    vcdc_uart_rx_head = 0;
    vcdc_uart_rx_tail = 0;
    usart_disable_tx_interrupt(VCDC_USART);
}

static size_t vcdc_uart_send_buffered(const uint8_t* data, size_t num_bytes) {
    size_t written = 0;
    while (!vcdc_uart_tx_full() && written < num_bytes) {
        vcdc_uart_tx_buffer[vcdc_uart_tx_tail % VCDC_UART_TX_BUFFER_SIZE] =
            data[written++];
        vcdc_uart_tx_tail++;
    }
    if (!vcdc_uart_tx_empty()) {
        usart_enable_tx_interrupt(VCDC_USART);
    }
    return written;
}

static size_t vcdc_uart_send_buffer_space(void) {
    return VCDC_UART_TX_BUFFER_SIZE -
           (uint16_t)(vcdc_uart_tx_tail - vcdc_uart_tx_head);
}

static size_t vcdc_uart_recv_buffered(uint8_t* data, size_t max_bytes) {
    size_t read = 0;
    while (!vcdc_uart_rx_empty() && read < max_bytes) {
        data[read++] = vcdc_uart_rx_buffer[
            vcdc_uart_rx_head % VCDC_UART_RX_BUFFER_SIZE];
        vcdc_uart_rx_head++;
    }
    return read;
}

static bool vcdc_uart_apply_line_coding(
    const struct usb_cdc_line_coding* coding) {
    uint32_t databits;
    if (coding->bDataBits == 7 || coding->bDataBits == 8) {
        databits = coding->bDataBits;
    } else {
        return false;
    }

    uint32_t stopbits;
    if (coding->bCharFormat == USB_CDC_1_STOP_BITS) {
        stopbits = USART_STOPBITS_1;
    } else if (coding->bCharFormat == USB_CDC_2_STOP_BITS) {
        stopbits = USART_STOPBITS_2;
    } else {
        return false;
    }

    uint32_t parity;
    if (coding->bParityType == USB_CDC_NO_PARITY) {
        parity = USART_PARITY_NONE;
    } else if (coding->bParityType == USB_CDC_ODD_PARITY) {
        parity = USART_PARITY_ODD;
    } else if (coding->bParityType == USB_CDC_EVEN_PARITY) {
        parity = USART_PARITY_EVEN;
    } else {
        return false;
    }

    usart_disable(VCDC_USART);
    if (parity != USART_PARITY_NONE) {
        /* libopencm3 counts the parity bit as one of the data bits. */
        databits++;
    }
    usart_set_baudrate(VCDC_USART, coding->dwDTERate);
    usart_set_databits(VCDC_USART, databits);
    usart_set_stopbits(VCDC_USART, stopbits);
    usart_set_parity(VCDC_USART, parity);
    usart_set_mode(VCDC_USART, USART_MODE_TX_RX);
    usart_set_flow_control(VCDC_USART, USART_FLOWCONTROL_NONE);
    usart_enable(VCDC_USART);

    vcdc_line_coding = *coding;
    return true;
}

static void vcdc_uart_setup(void) {
    rcc_periph_clock_enable(RCC_AFIO);
    rcc_periph_clock_enable(RCC_GPIOB);
    rcc_periph_clock_enable(VCDC_USART_CLOCK);

    /* USART1 remap: PB6 = TX, PB7 = RX. */
    gpio_primary_remap(AFIO_MAPR_SWJ_CFG_FULL_SWJ,
                       AFIO_MAPR_USART1_REMAP);
    gpio_set_mode(VCDC_USART_GPIO_PORT, GPIO_MODE_OUTPUT_50_MHZ,
                  GPIO_CNF_OUTPUT_ALTFN_PUSHPULL, VCDC_USART_GPIO_TX);
    gpio_set_mode(VCDC_USART_GPIO_PORT, GPIO_MODE_INPUT,
                  GPIO_CNF_INPUT_PULL_UPDOWN, VCDC_USART_GPIO_RX);
    gpio_set(VCDC_USART_GPIO_PORT, VCDC_USART_GPIO_RX);

    vcdc_uart_reset_buffers();
    usart_set_baudrate(VCDC_USART, DEFAULT_BAUDRATE);
    usart_set_databits(VCDC_USART, 8);
    usart_set_stopbits(VCDC_USART, USART_STOPBITS_1);
    usart_set_parity(VCDC_USART, USART_PARITY_NONE);
    usart_set_mode(VCDC_USART, USART_MODE_TX_RX);
    usart_set_flow_control(VCDC_USART, USART_FLOWCONTROL_NONE);
    usart_enable_rx_interrupt(VCDC_USART);
    nvic_enable_irq(VCDC_USART_NVIC_LINE);
    usart_enable(VCDC_USART);
}

void VCDC_USART_IRQ_NAME(void) {
    if (usart_get_flag(VCDC_USART, USART_FLAG_RXNE)) {
        uint8_t data = usart_recv(VCDC_USART);
        if (!vcdc_uart_rx_full()) {
            vcdc_uart_rx_buffer[vcdc_uart_rx_tail % VCDC_UART_RX_BUFFER_SIZE] = data;
            vcdc_uart_rx_tail++;
        }
    }

    if (usart_get_flag(VCDC_USART, USART_FLAG_TXE)) {
        if (!vcdc_uart_tx_empty()) {
            usart_send(VCDC_USART,
                       vcdc_uart_tx_buffer[vcdc_uart_tx_head % VCDC_UART_TX_BUFFER_SIZE]);
            vcdc_uart_tx_head++;
        } else {
            usart_disable_tx_interrupt(VCDC_USART);
        }
    }
}

#endif

static enum usbd_request_return_codes
vcdc_control_class_request(usbd_device *usbd_dev,
                           struct usb_setup_data *req,
                           uint8_t **buf, uint16_t *len,
                           usbd_control_complete_callback* complete) {
    (void)complete;
    (void)usbd_dev;
    (void)buf;
    (void)len;

    if (req->wIndex != INTF_VCDC_DATA && req->wIndex != INTF_VCDC_COMM) {
        return USBD_REQ_NEXT_CALLBACK;
    }
    enum usbd_request_return_codes status = USBD_REQ_NOTSUPP;

    switch (req->bRequest) {
        case USB_CDC_REQ_SET_CONTROL_LINE_STATE: {
            /*
             * This Linux cdc_acm driver requires this to be implemented
             * even though it's optional in the CDC spec, and we don't
             * advertise it in the ACM functional descriptor.
             */

            status = USBD_REQ_HANDLED;
            break;
        }
        case USB_CDC_REQ_SET_LINE_CODING: {
            if (*len < sizeof(struct usb_cdc_line_coding)) {
                status = USBD_REQ_NOTSUPP;
#if VCDC_UART_BRIDGE_AVAILABLE
            } else {
                status = vcdc_uart_apply_line_coding(
                    (const struct usb_cdc_line_coding *)(*buf))
                    ? USBD_REQ_HANDLED : USBD_REQ_NOTSUPP;
#else
            } else {
                /* Accept whatever is requested when no UART is attached. */
                status = USBD_REQ_HANDLED;
#endif
            }
            break;
        }
        case USB_CDC_REQ_GET_LINE_CODING: {
            struct usb_cdc_line_coding *coding;
            coding = (struct usb_cdc_line_coding*)(*buf);
#if VCDC_UART_BRIDGE_AVAILABLE
            *coding = vcdc_line_coding;
#else
            /* Send back a dummy default coding */
            coding->dwDTERate = DEFAULT_BAUDRATE;
            coding->bCharFormat = USB_CDC_1_STOP_BITS;
            coding->bParityType = USB_CDC_NO_PARITY;
            coding->bDataBits = 8;
#endif
            *len = sizeof(struct usb_cdc_line_coding);
            status = USBD_REQ_HANDLED;
            break;
        }
        default: {
            status = USBD_REQ_NOTSUPP;
            break;
        }
    }

    return status;
}

/* Receive data from the host */
static void vcdc_bulk_data_out(usbd_device *usbd_dev, uint8_t ep) {
    uint8_t buf[USB_VCDC_MAX_PACKET_SIZE];
    uint16_t len = usbd_ep_read_packet(usbd_dev, ep, (void*)buf, sizeof(buf));

    uint16_t i;
    for (i=0; i < len && !vcdc_rx_buffer_full(); i++) {
        vcdc_rx_buffer_put(buf[i]);
    }
    
    if (len > 0 && (vcdc_rx_callback != NULL)) {
        vcdc_rx_callback();
    }
}

static void vcdc_set_config(usbd_device *usbd_dev, uint16_t wValue) {
    (void)wValue;

    usbd_ep_setup(usbd_dev, ENDP_VCDC_DATA_OUT, USB_ENDPOINT_ATTR_BULK,
                  USB_VCDC_MAX_PACKET_SIZE,
                  vcdc_bulk_data_out);
    usbd_ep_setup(usbd_dev, ENDP_VCDC_DATA_IN, USB_ENDPOINT_ATTR_BULK,
                  USB_VCDC_MAX_PACKET_SIZE,
                  NULL);
    usbd_ep_setup(usbd_dev, ENDP_VCDC_COMM_IN, USB_ENDPOINT_ATTR_INTERRUPT, 16, NULL);

    cmp_usb_register_control_class_callback(INTF_VCDC_DATA, vcdc_control_class_request);
    cmp_usb_register_control_class_callback(INTF_VCDC_COMM, vcdc_control_class_request);
}

static uint16_t packet_len = 0;
static uint8_t packet_buffer[USB_VCDC_MAX_PACKET_SIZE];

static void vcdc_app_reset(void) {
    packet_len = 0;
    vcdc_tx_head = 0;
    vcdc_tx_tail = 0;
    vcdc_rx_head = 0;
    vcdc_rx_tail = 0;
#if VCDC_UART_BRIDGE_AVAILABLE
    vcdc_uart_reset_buffers();
#endif
}

static usbd_device* vcdc_usbd_dev;

void vcdc_app_setup(usbd_device* usbd_dev,
                    GenericCallback vcdc_tx_cb,
                    GenericCallback vcdc_rx_cb) {
    vcdc_usbd_dev = usbd_dev;
    vcdc_tx_callback = vcdc_tx_cb;
    vcdc_rx_callback = vcdc_rx_cb;

#if VCDC_UART_BRIDGE_AVAILABLE
    vcdc_uart_setup();
#endif

    cmp_usb_register_set_config_callback(vcdc_set_config);
    cmp_usb_register_reset_callback(vcdc_app_reset);
}

bool vcdc_app_update(void) {
    bool active = false;

#if VCDC_UART_BRIDGE_AVAILABLE
    /* Move host -> VCDC RX FIFO -> USART1 TX FIFO. */
    while (!vcdc_rx_buffer_empty() && vcdc_uart_send_buffer_space() > 0) {
        uint8_t bridge_buffer[USB_VCDC_MAX_PACKET_SIZE];
        size_t limit = vcdc_uart_send_buffer_space();
        if (limit > sizeof(bridge_buffer)) {
            limit = sizeof(bridge_buffer);
        }
        size_t count = vcdc_recv_buffered(bridge_buffer, limit);
        if (count == 0) {
            break;
        }
        vcdc_uart_send_buffered(bridge_buffer, count);
        active = true;
    }

    /* Move USART1 RX FIFO -> VCDC TX FIFO. */
    while (!vcdc_uart_rx_empty() && vcdc_send_buffer_space() > 0) {
        uint8_t bridge_buffer[USB_VCDC_MAX_PACKET_SIZE];
        size_t limit = vcdc_send_buffer_space();
        if (limit > sizeof(bridge_buffer)) {
            limit = sizeof(bridge_buffer);
        }
        size_t count = vcdc_uart_recv_buffered(bridge_buffer, limit);
        if (count == 0) {
            break;
        }
        vcdc_send_buffered(bridge_buffer, count);
        active = true;
    }
#endif

    while (packet_len < USB_VCDC_MAX_PACKET_SIZE && !vcdc_tx_buffer_empty()) {
        packet_buffer[packet_len] = vcdc_tx_buffer_get();
        packet_len++;
    }

    if (packet_len > 0 && cmp_usb_configured()) {
        uint16_t sent = usbd_ep_write_packet(vcdc_usbd_dev, ENDP_VCDC_DATA_IN,
                                             (const void*)packet_buffer,
                                             packet_len);
        
        if (sent != 0) {
            packet_len = 0;
            active = true;
            if (vcdc_tx_callback != NULL) {
                vcdc_tx_callback();
            }
        }
    }
    
    return active;
}

void vcdc_putchar(const char c) {
    if (!vcdc_tx_buffer_full()) {
        vcdc_tx_buffer_put(c);
    }
}

void vcdc_print(const char* s) {
    while (*s != '\0') {
        vcdc_putchar(*s++);
    }
}

void vcdc_println(const char* s) {
    while (*s != '\0') {
        vcdc_putchar(*s++);
    }
    vcdc_putchar('\r');
    vcdc_putchar('\n');
}

void vcdc_print_hex_nibble(uint8_t x) {
    uint8_t nibble = x & 0x0F;
    char nibble_char;
    if (nibble < 10) {
        nibble_char = '0' + nibble;
    } else {
        nibble_char = 'A' + (nibble - 10);
    }
    vcdc_putchar(nibble_char);
}

void vcdc_print_hex_byte(uint8_t x) {
    vcdc_print_hex_nibble(x >> 4);
    vcdc_print_hex_nibble(x);
}

void vcdc_print_hex(uint32_t x) {
    vcdc_print_hex_nibble((uint8_t)(x >> 28));
    vcdc_print_hex_nibble((uint8_t)(x >> 24));
    vcdc_print_hex_nibble((uint8_t)(x >> 20));
    vcdc_print_hex_nibble((uint8_t)(x >> 16));
    vcdc_print_hex_nibble((uint8_t)(x >> 12));
    vcdc_print_hex_nibble((uint8_t)(x >> 8));
    vcdc_print_hex_nibble((uint8_t)(x >> 4));
    vcdc_print_hex_nibble((uint8_t)(x >> 0));
}

#endif
